import os
import sys
import logging
from contextlib import nullcontext

import json
import re
import time
import librosa
import torch
import torchaudio
from torch.nn.utils.rnn import pad_sequence

import warnings

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)

from omegaconf import OmegaConf

from indextts.gpt.model_v2 import UnifiedVoice
from indextts.utils.maskgct_utils import build_semantic_model, build_semantic_codec
from indextts.utils.checkpoint import load_checkpoint
from indextts.utils.front import TextNormalizer, TextTokenizer
from indextts.utils.subtitle import cumulative_slices, segment_texts

from indextts.s2mel.modules.commons import load_checkpoint2, MyModel
from indextts.s2mel.modules.bigvgan import bigvgan
from indextts.s2mel.modules.campplus.DTDNN import CAMPPlus
from indextts.s2mel.modules.audio import mel_spectrogram

from transformers import AutoTokenizer
from modelscope import AutoModelForCausalLM
import safetensors
from transformers import SeamlessM4TFeatureExtractor
from transformers.generation.stopping_criteria import StoppingCriteria, StoppingCriteriaList
import random
import torch.nn.functional as F

logger = logging.getLogger("indextts.infer_v2")
if os.environ.get("INDEXTTS_DEBUG_BATCH"):
    # 本项目自有的批处理调试输出：env 开启时强制 DEBUG 级。server 语境下
    # 记录经 propagate 流进 "indextts" 的文件+控制台 handler（传播只查本源
    # logger 的级别，不会被父级 INFO 挡掉）；CLI 独立运行无人配置 logging
    # （lastResort 只发 WARNING 以上），补挂 stderr handler 兜底。
    logger.setLevel(logging.DEBUG)
    if not logging.getLogger("indextts").handlers and not logging.getLogger().handlers:
        _dbg_handler = logging.StreamHandler(sys.stderr)
        _dbg_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(_dbg_handler)


def _report(msg):
    """耗时/落盘等运行信息：接上日志系统（server.py）时走 logger.info 进
    logs/server.log；CLI 没人配置 logging 时仍旧 print 到终端。"""
    if logger.isEnabledFor(logging.INFO):
        logger.info(msg)
    else:
        print(msg)

class IndexTTS2:
    def __init__(
            self, cfg_path="checkpoints/config.yaml", model_dir="checkpoints", use_fp16=False, device=None,
            use_s2mel_fp16=False, use_w2v_fp16=False, use_qwen_fp16=True, aux_paths=None,
            gpt_batch_size=0, gpt_batch_max_gib=0.0
    ):
        """
        Args:
            cfg_path (str): path to the config file.
            model_dir (str): path to the model directory.
            use_fp16 (bool): whether to use fp16.
            device (str): device to use (e.g., 'cuda:0', 'cpu'). If None, it will be set automatically based on the availability of CUDA or MPS.
            use_s2mel_fp16 (bool): whether to run the s2mel (diffusion) model in FP16. Speeds up
                CFM flow-matching inference and cuts VRAM, at the cost of slight quality trade-off.
            aux_paths (dict | None): pre-downloaded auxiliary model paths from ensure_models_available().
                If None, downloads are performed automatically.
            use_w2v_fp16 (bool): run the w2v-bert-2.0 semantic encoder in FP16. Saves ~1GB VRAM
                on 8G cards; default False to avoid any numerical/quality regression.
            use_qwen_fp16 (bool): run the Qwen emotion model in FP16. Default True (it already
                loads as float16); set False to fall back to FP32 for debugging.
            gpt_batch_size (int): 非流式合成时，把最多这么多段文本拼进一次 GPT 生成
                （段级批处理）。0/1 = 关闭（逐段生成，旧行为）。见 docs/PERFORMANCE_zh.md。
            gpt_batch_max_gib (float): 段级批处理允许给 KV cache 的显存上限（GiB）。**0 = 自动**
                （按当前空闲显存减 1.5GiB 安全余量推算），只在要手动压低/抬高时填。
                批处理会把 `行数 × beam 数 × max_mel_tokens` 条 cache 同时留在显存里，8G 卡上
                b=4（12 行 beam）跑满 1500 token 要 2.2GB，实测被 Windows 显存换页拖到每步
                ~370ms（比不批处理慢 8 倍），所以按预算反推最大行数并自动降级。
        """
        # Ensure auxiliary models are available
        if aux_paths is None:
            from indextts.utils.model_download import ensure_models_available
            aux_paths = ensure_models_available(model_dir)

        if device is not None:
            self.device = device
            self.use_fp16 = False if device == "cpu" else use_fp16
        elif torch.cuda.is_available():
            self.device = "cuda:0"
            self.use_fp16 = use_fp16
        elif hasattr(torch, "xpu") and torch.xpu.is_available():
            self.device = "xpu"
            self.use_fp16 = use_fp16
        elif hasattr(torch, "mps") and torch.backends.mps.is_available():
            self.device = "mps"
            self.use_fp16 = False  # Use float16 on MPS is overhead than float32
        else:
            self.device = "cpu"
            self.use_fp16 = False
            print(">> Be patient, it may take a while to run in CPU mode.")

        self.cfg = OmegaConf.load(cfg_path)
        self.model_dir = model_dir
        self.dtype = torch.float16 if self.use_fp16 else None
        self.stop_mel_token = self.cfg.gpt.stop_mel_token
        self.use_s2mel_fp16 = use_s2mel_fp16
        self.use_w2v_fp16 = use_w2v_fp16
        self.use_qwen_fp16 = use_qwen_fp16
        # s2mel CFM 推理参数，可用 generation_kwargs 或实例属性覆盖
        self.diffusion_steps = 25
        self.inference_cfg_rate = 0.7
        # 段级批处理上限（0/1 = 关闭）。逐段生成时每步几乎全是 CPU 提交开销，
        # 拼 batch 后每行每 token 的成本会明显下降，详见 docs/PERFORMANCE_zh.md。
        self.gpt_batch_size = int(gpt_batch_size or 0)
        self.gpt_batch_max_gib = float(gpt_batch_max_gib or 0.0)
        # 最近一次生成实际怎么批的（mode/rows/groups/降级原因），/model/state 透给前端。
        # 只在生成时才知道真实行数（取决于当时的空闲显存与段数），所以不能只靠参数回显。
        self.gpt_batch_last = None

        self.gpt = UnifiedVoice(**self.cfg.gpt)
        self.gpt_path = os.path.join(self.model_dir, self.cfg.gpt_checkpoint)
        load_checkpoint(self.gpt, self.gpt_path)
        self.gpt = self.gpt.to(self.device)
        if self.use_fp16:
            self.gpt.eval().half()
        else:
            self.gpt.eval()
        print(">> GPT weights restored from:", self.gpt_path)

        self.gpt.post_init_gpt2_config(kv_cache=True, half=self.use_fp16)

        # Load w2v-bert-2.0 from pre-downloaded local dir
        w2v_bert_dir = aux_paths["w2v_bert"]
        self.extract_features = SeamlessM4TFeatureExtractor.from_pretrained(w2v_bert_dir, local_files_only=True)
        self.semantic_model, self.semantic_mean, self.semantic_std = build_semantic_model(
            os.path.join(self.model_dir, self.cfg.w2v_stat),
            model_path=w2v_bert_dir)
        self.semantic_model = self.semantic_model.to(self.device)
        self.semantic_model.eval()
        self.semantic_mean = self.semantic_mean.to(self.device)
        self.semantic_std = self.semantic_std.to(self.device)
        if self.use_w2v_fp16 and self.device.startswith(("cuda", "xpu", "mps")):
            self.semantic_model = self.semantic_model.half()
            # mean/std stay fp32 (a few MB, negligible): get_emb normalizes in
            # float32 so the output dtype stays identical to the fp32 path.
            print(">> w2v-bert converted to FP16")

        semantic_codec = build_semantic_codec(self.cfg.semantic_codec)
        semantic_code_ckpt = aux_paths["semantic_codec"]
        safetensors.torch.load_model(semantic_codec, semantic_code_ckpt)
        self.semantic_codec = semantic_codec.to(self.device)
        self.semantic_codec.eval()
        print('>> semantic_codec weights restored from: {}'.format(semantic_code_ckpt))

        s2mel_path = os.path.join(self.model_dir, self.cfg.s2mel_checkpoint)
        s2mel = MyModel(self.cfg.s2mel, use_gpt_latent=True)
        s2mel, _, _, _ = load_checkpoint2(
            s2mel,
            None,
            s2mel_path,
            load_only_params=True,
            ignore_modules=[],
            is_distributed=False,
        )
        self.s2mel = s2mel.to(self.device)
        self.s2mel.models['cfm'].estimator.setup_caches(max_batch_size=1, max_seq_length=8192)

        if self.use_s2mel_fp16 and self.device.startswith(("cuda", "xpu", "mps")):
            self.s2mel = self.s2mel.half()
            print(">> s2mel converted to FP16")
        
        self.s2mel.eval()
        print(">> s2mel weights restored from:", s2mel_path)

        # load campplus_model
        campplus_ckpt_path = aux_paths["campplus"]
        campplus_model = CAMPPlus(feat_dim=80, embedding_size=192)
        campplus_model.load_state_dict(torch.load(campplus_ckpt_path, map_location="cpu"))
        self.campplus_model = campplus_model.to(self.device)
        self.campplus_model.eval()
        print(">> campplus_model weights restored from:", campplus_ckpt_path)

        # load BigVGAN from pre-downloaded local dir
        bigvgan_dir = aux_paths["bigvgan"]
        self.bigvgan = bigvgan.BigVGAN.from_pretrained(bigvgan_dir)
        self.bigvgan = self.bigvgan.to(self.device)
        self.bigvgan.remove_weight_norm()
        self.bigvgan.eval()
        print(">> bigvgan weights restored from:", bigvgan_dir)

        self.bpe_path = os.path.join(self.model_dir, self.cfg.dataset["bpe_model"])
        self.normalizer = TextNormalizer(enable_glossary=True)
        self.normalizer.load()
        print(">> TextNormalizer loaded")
        self.tokenizer = TextTokenizer(self.bpe_path, self.normalizer)
        print(">> bpe model loaded from:", self.bpe_path)

        # 加载术语词汇表（如果存在）
        self.glossary_path = os.path.join(self.model_dir, "glossary.yaml")
        if os.path.exists(self.glossary_path):
            self.normalizer.load_glossary_from_yaml(self.glossary_path)
            print(">> Glossary loaded from:", self.glossary_path)

        # map_location="cpu"：权重若是在 CUDA 上保存的，CPU-only 机器直接
        # RuntimeError；下面 .to(self.device) 才是决定最终位置的步骤。
        emo_matrix = torch.load(os.path.join(self.model_dir, self.cfg.emo_matrix), map_location="cpu")
        self.emo_matrix = emo_matrix.to(self.device)
        self.emo_num = list(self.cfg.emo_num)

        spk_matrix = torch.load(os.path.join(self.model_dir, self.cfg.spk_matrix), map_location="cpu")
        self.spk_matrix = spk_matrix.to(self.device)

        self.emo_matrix = torch.split(self.emo_matrix, self.emo_num)
        self.spk_matrix = torch.split(self.spk_matrix, self.emo_num)

        mel_fn_args = {
            "n_fft": self.cfg.s2mel['preprocess_params']['spect_params']['n_fft'],
            "win_size": self.cfg.s2mel['preprocess_params']['spect_params']['win_length'],
            "hop_size": self.cfg.s2mel['preprocess_params']['spect_params']['hop_length'],
            "num_mels": self.cfg.s2mel['preprocess_params']['spect_params']['n_mels'],
            "sampling_rate": self.cfg.s2mel["preprocess_params"]["sr"],
            "fmin": self.cfg.s2mel['preprocess_params']['spect_params'].get('fmin', 0),
            "fmax": None if self.cfg.s2mel['preprocess_params']['spect_params'].get('fmax', "None") == "None" else 8000,
            "center": False
        }
        self.mel_fn = lambda x: mel_spectrogram(x, **mel_fn_args)

        # 缓存参考音频：
        self.cache_spk_cond = None
        self.cache_s2mel_style = None
        self.cache_s2mel_prompt = None
        self.cache_spk_audio_prompt = None
        self.cache_emo_cond = None
        self.cache_emo_audio_prompt = None
        self.cache_mel = None

        # Qwen 情感模型懒加载：只在用到「情绪文本」模式（use_emo_text）时才初始化。
        # 直接在这里加载会让每次加载主模型都白占 ~1.57GB 显存，而默认的 emoMode=1
        # （用参考音频带情绪）根本用不到它。改由 qwen_emo 属性在首次访问时建出来。
        self._qwen_emo = None

        # 进度引用显示（可选）
        self.gr_progress = None
        # 上一次生成是否遇到文本规范化失败并退回原文（读音可能不标准）。
        # server 读取后随 done 事件提示前端，详见 infer_generator 中 tokenize 之后。
        self.text_normalize_fallback = None
        # 最近一次生成的段边界快照（字幕用）。None = 这次没记上，前端不出字幕。
        self.segment_slices = None
        self.segment_text_method = None
        # 段内各子阶段耗时占比（gpt生成/gpt前向/s2mel/bigvgan），每次生成后 EMA 自校准
        self._phase_ema = None
        self.model_version = self.cfg.version if hasattr(self.cfg, "version") else None

    def _phase_w(self):
        if self._phase_ema is None:
            return (0.55, 0.07, 0.23, 0.15)
        return tuple(self._phase_ema)

    def _kv_bytes_per_token(self):
        """单条 KV cache 每 token 占的字节数（决定段级批处理能开几行）。

        每条 cache = n_layer 层 × (K, V) × n_embd 维 × dtype 字节数。fp16 时
        24 层/1280 维 → 24×2×1280×2 = 122,880 B/token ≈ 120KB；1500 token 就是
        180MB/条，乘上 beam 数和行数很容易把 8G 卡顶爆。这里从模型 config 读，
        读不到就按 IndexTTS2 的默认结构估。
        """
        cfg = getattr(getattr(self.gpt, "inference_model", None), "config", None)
        n_layer = int(getattr(cfg, "n_layer", None) or 24)
        n_embd = int(getattr(cfg, "n_embd", None) or 1280)
        # 用真实参数的 dtype 判断（`self.gpt.dtype` 在有非浮点缓存时不可靠，曾把 fp16
        # 模型算成 4 字节/token，护栏因此放宽一倍、b=4 又变成单行）。
        dtype = getattr(self.gpt, "dtype", None)
        try:
            for p in self.gpt.parameters():
                if p.is_floating_point():
                    dtype = p.dtype
                    break
        except AttributeError:
            pass  # 测试用的 stub 可能没有 parameters()
        item = 2 if dtype in (torch.float16, torch.bfloat16) else 4
        return n_layer * 2 * n_embd * item

    def _autocast_s2mel(self):
        """s2mel（扩散 CFM）的 autocast 上下文：按 use_s2mel_fp16 决定是否 FP16。"""
        if not self.use_s2mel_fp16:
            return nullcontext()
        device_type = self.device.split(":")[0]
        if device_type not in ("cuda", "cpu", "xpu", "mps", "mtia"):
            device_type = "cpu"
        return torch.amp.autocast(device_type=device_type, enabled=True, dtype=torch.float16)

    @property
    def qwen_emo(self):
        """Qwen 情感模型，用到才初始化（懒加载）。

        直接在 __init__ 里建会让每次加载主模型都白占 ~1.57GB 显存，而它只在
        use_emo_text（前端 emoMode=3，情绪文本模式）时才被访问。默认的
        emoMode=1 用参考音频带情绪，根本用不到它。首次访问才 from_pretrained
        （多等 ~1.2s，之后正常）。
        """
        if self._qwen_emo is None:
            self._qwen_emo = QwenEmotion(
                os.path.join(self.model_dir, self.cfg.qwen_emo_path),
                use_fp16=self.use_qwen_fp16,
            )
        return self._qwen_emo

    @torch.no_grad()
    def get_emb(self, input_features, attention_mask):
        # w2v_fp16: feed the input in the model's own dtype (no-op when fp32),
        # and normalize the output in float32 so downstream dtype is unchanged.
        vq_emb = self.semantic_model(
            input_features=input_features.to(self.semantic_model.dtype),
            attention_mask=attention_mask,
            output_hidden_states=True,
        )
        feat = vq_emb.hidden_states[17]  # (B, T, C)
        feat = feat.float()
        feat = (feat - self.semantic_mean) / self.semantic_std
        return feat

    def remove_long_silence(self, codes: torch.Tensor, silent_token=52, max_consecutive=30):
        """
        Shrink special tokens (silent_token and stop_mel_token) in codes
        codes: [B, T]
        """
        code_lens = []
        codes_list = []
        device = codes.device
        dtype = codes.dtype
        isfix = False
        for i in range(0, codes.shape[0]):
            code = codes[i]
            if not torch.any(code == self.stop_mel_token).item():
                len_ = code.size(0)
            else:
                stop_mel_idx = (code == self.stop_mel_token).nonzero(as_tuple=False)
                len_ = stop_mel_idx[0].item() if len(stop_mel_idx) > 0 else code.size(0)

            count = torch.sum(code == silent_token).item()
            if count > max_consecutive:
                # code = code.cpu().tolist()
                ncode_idx = []
                n = 0
                for k in range(len_):
                    assert code[
                               k] != self.stop_mel_token, f"stop_mel_token {self.stop_mel_token} should be shrinked here"
                    if code[k] != silent_token:
                        ncode_idx.append(k)
                        n = 0
                    elif code[k] == silent_token and n < 10:
                        ncode_idx.append(k)
                        n += 1
                    # if (k == 0 and code[k] == 52) or (code[k] == 52 and code[k-1] == 52):
                    #    n += 1
                # new code
                len_ = len(ncode_idx)
                codes_list.append(code[ncode_idx])
                isfix = True
            else:
                # shrink to len_
                codes_list.append(code[:len_])
            code_lens.append(len_)
        if isfix:
            if len(codes_list) > 1:
                codes = pad_sequence(codes_list, batch_first=True, padding_value=self.stop_mel_token)
            else:
                codes = codes_list[0].unsqueeze(0)
        else:
            # unchanged
            pass
        # clip codes to max length
        max_len = max(code_lens)
        if max_len < codes.shape[1]:
            codes = codes[:, :max_len]
        code_lens = torch.tensor(code_lens, dtype=torch.long, device=device)
        return codes, code_lens

    def interval_silence(self, wavs, sampling_rate=22050, interval_silence=200):
        """
        Silences to be insert between generated segments.
        """

        if not wavs:
            return wavs
        if interval_silence <= 0:
            # 恒返回 tensor：以前这里把 list 原样返回，流式路径于是会 yield
            # 「tensor 或 list」两种类型，消费方容易踩坑。
            return torch.zeros(wavs[0].size(0), 0)

        # get channel_size
        channel_size = wavs[0].size(0)
        # get silence tensor
        sil_dur = int(sampling_rate * interval_silence / 1000.0)
        return torch.zeros(channel_size, sil_dur)

    def insert_interval_silence(self, wavs, sampling_rate=22050, interval_silence=200):
        """
        Insert silences between generated segments.
        wavs: List[torch.tensor]
        """

        if not wavs or interval_silence <= 0:
            return wavs

        # get channel_size
        channel_size = wavs[0].size(0)
        # get silence tensor
        sil_dur = int(sampling_rate * interval_silence / 1000.0)
        sil_tensor = torch.zeros(channel_size, sil_dur)

        wavs_list = []
        for i, wav in enumerate(wavs):
            wavs_list.append(wav)
            if i < len(wavs) - 1:
                wavs_list.append(sil_tensor)

        return wavs_list

    def _record_segment_slices(self, wavs, seg_token_lists, text, sampling_rate, interval_silence):
        """把「每段在成品音频里的起止秒 + 对应的原文片段」存进 self.segment_slices。

        字幕靠这个快照把文本对齐到时间轴。时间是【物理】的：成品 = 各段波形 +
        段间静音拼接，所以起止秒由各段样本数累加得出，不是按字数估算。文本走
        subtitle.segment_texts 锚回【用户原文】（词元流是规范化后的大写英文 +
        半角标点，直接当字幕没法看）。

        这是附属产物：任何异常都吞掉并置 None，绝不让一次已成功的生成变成报错。
        段数对不上时也置 None —— 宁可没有字幕，也不要文本和时间错位的字幕。
        """
        self.segment_slices = None
        self.segment_text_method = None
        try:
            if len(wavs) != len(seg_token_lists):
                logger.warning("Subtitle skipped: %d audio segments vs %d token segments",
                               len(wavs), len(seg_token_lists))
                return
            seg_samples = [int(w.shape[-1]) for w in wavs]
            boundaries = cumulative_slices(seg_samples, sampling_rate, interval_silence)
            texts, method = segment_texts(text, seg_token_lists)
            if len(texts) != len(boundaries):
                logger.warning("Subtitle skipped: %d texts vs %d boundaries",
                               len(texts), len(boundaries))
                return
            self.segment_slices = [
                {"index": i, "start": start, "end": end, "text": texts[i]}
                for i, (start, end) in enumerate(boundaries)
            ]
            self.segment_text_method = method
        except Exception as e:  # noqa: BLE001 - 字幕失败不能影响音频
            self.segment_slices = None
            self.segment_text_method = None
            logger.warning("Subtitle slice recording failed: %s", e, exc_info=True)

    def _set_gr_progress(self, value, desc):
        if self.gr_progress is not None:
            self.gr_progress(value, desc=desc)

    def _load_and_cut_audio(self,audio_path,max_audio_length_seconds,verbose=False,sr=None):
        if not sr:
            audio, sr = librosa.load(audio_path)
        else:
            audio, _ = librosa.load(audio_path,sr=sr)
        audio = torch.tensor(audio).unsqueeze(0)
        max_audio_samples = int(max_audio_length_seconds * sr)

        if audio.shape[1] > max_audio_samples:
            if verbose:
                print(f"Audio too long ({audio.shape[1]} samples), truncating to {max_audio_samples} samples")
            audio = audio[:, :max_audio_samples]
        return audio, sr
    
    def normalize_emo_vec(self, emo_vector, apply_bias=True):
        # apply biased emotion factors for better user experience,
        # by de-emphasizing emotions that can cause strange results
        if apply_bias:
            # [happy, angry, sad, afraid, disgusted, melancholic, surprised, calm]
            emo_bias = [0.9375, 0.875, 1.0, 1.0, 0.9375, 0.9375, 0.6875, 0.5625]
            emo_vector = [vec * bias for vec, bias in zip(emo_vector, emo_bias)]

        # the total emotion sum must be 0.8 or less
        emo_sum = sum(emo_vector)
        if emo_sum > 0.8:
            scale_factor = 0.8 / emo_sum
            emo_vector = [vec * scale_factor for vec in emo_vector]

        return emo_vector

    def _gpt_batch_budget_bytes(self):
        """段级批处理允许给 KV cache 用的字节预算。

        交接简报的硬约束：同类代码要跑 6G/8G/12G/16G/24G，涉及显存的量不能写死，必须按
        真实空闲显存自适应。所以默认（`gpt_batch_max_gib=0`）按当前空闲显存减去安全余量：
        余量用来吸收"批处理期间 cached_mel_emb/prefill 激活"和 Windows 桌面占用，8G 卡实测
        3 行（1.6GiB）稳、4 行（2.1GiB）会被换页拖慢 8 倍，这个公式正好落在 3 行，6G 卡则
        退化成 1 行（等于不批处理）。
        """
        fixed = float(getattr(self, "gpt_batch_max_gib", 0) or 0)
        if fixed > 0:
            return int(fixed * (1024 ** 3))
        device_type = str(self.device).split(":")[0]
        if device_type != "cuda" or not torch.cuda.is_available():
            return 0  # 非 CUDA 设备不做显存推算，交给调用方决定
        try:
            free, _total = torch.cuda.mem_get_info(torch.device(self.device))
        except Exception as e:
            if os.environ.get("INDEXTTS_DEBUG_BATCH"):
                logger.debug(f">> mem_get_info failed ({e}), fallback 1.5GiB budget")
            return 1536 * 1024 * 1024
        headroom = 1024 * 1024 * 1024  # 1.0GiB：8G 卡实测预生成前 free≈2.6GiB → 1.6GiB 预算 → 3 行
        return max(0, int(free) - headroom)

    def _auto_gpt_batch_size(self, gen_params):
        """`gpt_batch_size=-1`（自动）时，按当前空闲显存决定这一轮拼几段。

        与 `_gpt_batch_budget_bytes` 的区别：那个是"用户指定的行数不许超过多少"的**上限**，
        这个是**主动求出我的卡能吃几行**。所以不设死板的小上限，卡大就多拼（上限只是防止
        单次 batch 大到 prefill 激活/attention 矩阵本身也吃满显存）。

        余量只取一次：预算已经是"空闲减 1.0 GiB"，这里不再额外打折。此前这里又乘了
        0.75，等于双重余量，8G 卡算出来只有 2 行，与实测甜点（3 行、峰值 5.3 GiB）不符，
        白白少一行的收益。真正生成时 `_precompute_segment_codes` 还会按当时的空闲显存
        再夹一次，安全网没有少。
        """
        if str(self.device).split(":")[0] != "cuda" or not torch.cuda.is_available():
            return 1
        try:
            free, _total = torch.cuda.mem_get_info(torch.device(self.device))
        except Exception:
            return 1
        usable = int(free) - 1024 * 1024 * 1024  # 1.0GiB 安全余量（桌面占用/分配器碎片）
        if usable <= 0:
            return 1
        _beams = max(1, int(gen_params.get("num_beams", 1) or 1))
        _max_gen = int(gen_params.get("max_generate_length", 1500) or 1500)
        _bytes_per_row = self._kv_bytes_per_token() * _max_gen * _beams
        if _bytes_per_row <= 0:
            return 1
        rows = int(usable // _bytes_per_row)
        return max(1, min(rows, 12))

    def gpt_batch_preview(self, num_beams=3, max_generate_length=1500, refresh=False):
        """给 UI 用：按当前空闲显存，自动模式在本机会拼几行（不触发任何生成）。

        用默认的 3 beam / 1500 token 估算，与 server 的默认表单一致；用户改了
        beam 或 max_mel 时真实行数会变，所以前端同时展示 gpt_batch_last（实际值）。

        refresh=True 时先清一次 torch 缓存块再读空闲量：Windows 上分配器不会把
        上一轮生成的临时显存还给驱动，不清的话同一张 8G 卡在首次生成后会从
        "3 行"掉到"1 行"，用户看到的就是假数字。
        """
        if str(self.device).split(":")[0] != "cuda" or not torch.cuda.is_available():
            return {"auto_rows": 1, "budget_gib": 0.0, "free_gib": 0.0}
        if refresh:
            try:
                torch.cuda.empty_cache()
            except Exception as e:
                if os.environ.get("INDEXTTS_DEBUG_BATCH"):
                    logger.debug(f">> empty_cache failed ({e})")
        rows = self._auto_gpt_batch_size({
            "num_beams": int(num_beams or 3),
            "max_generate_length": int(max_generate_length or 1500),
        })
        try:
            free, _ = torch.cuda.mem_get_info(torch.device(self.device))
            free_gib = round(int(free) / 1024 ** 3, 2)
        except Exception:
            free_gib = 0.0
        return {
            "auto_rows": int(rows),
            "budget_gib": round(self._gpt_batch_budget_bytes() / 1024 ** 3, 2),
            "free_gib": free_gib,
        }

    def _precompute_segment_codes(self, spk_cond_emb, emo_cond_emb, emovec, token_id_lists,
                                  gen_params, batch_size=4, length_ratio=4.0, progress=None,
                                  mode="manual"):
        """非流式路径的段级批处理：把长度相近的若干段拼成一个 batch，一次 generate 出全部 mel codes。

        段越长解码步越多，而组内所有行必须一起跑到最慢那行结束，因此按文本长度排序后贪心分组
        （组内 max/min 长度比不超过 length_ratio），避免长段被大量短段拖住。
        实测每多一行只多 ~1-2ms/token，而单独生成一小段要 ~13.8ms/token，所以即使长度不太齐
        拼批通常仍然划算；length_ratio 只是"极端不齐"时的保护。

        Returns:
            dict[int, torch.Tensor]: 段序号 -> 该段的 codes 行，形状 (1, T)。
        """
        _rows_requested = int(batch_size)
        order = sorted(range(len(token_id_lists)), key=lambda i: len(token_id_lists[i]))
        # 显存护栏：批处理期间同时存活「行数 × beam 数」条 KV cache，每条长到 max_generate_length
        # 就是 120KB/token（24 层 × K/V × 1280 维 × fp16）。8G 卡上 12 行 beam 跑满 1500 token
        # 需要 2.2GB，实测会被 Windows 显存换页拖成每步 ~370ms（比逐段还慢 8 倍），所以这里按
        # 预算反推能开几行，超了就自动降级而不是硬上。
        _beams = int(gen_params.get("num_beams", 1) or 1)
        _max_gen = int(gen_params.get("max_generate_length", 1500) or 1500)
        _bytes_per_row = self._kv_bytes_per_token() * _max_gen * max(1, _beams)
        _budget = self._gpt_batch_budget_bytes()
        if _budget > 0 and _bytes_per_row > 0:
            _max_rows = max(1, int(_budget // _bytes_per_row))
            if batch_size > _max_rows:
                if os.environ.get("INDEXTTS_DEBUG_BATCH"):
                    logger.debug(f">> gpt batch rows clamped {batch_size} -> {_max_rows} "
                                 f"(budget {_budget / 1024 ** 3:.2f}GiB, "
                                 f"{_bytes_per_row / 1024 ** 2:.0f}MiB per row @ {_max_gen} tokens "
                                 f"× {_beams} beams)")
                if os.environ.get("INDEXTTS_DEBUG_BATCH") and str(self.device).startswith("cuda"):
                    _f, _t = torch.cuda.mem_get_info(torch.device(self.device))
                    logger.debug(f">> gpt batch mem: free={_f / 1024 ** 3:.2f}GiB")
                batch_size = _max_rows

        groups = []
        for i in order:
            if groups:
                g = groups[-1]
                lens = [len(token_id_lists[j]) for j in g] + [len(token_id_lists[i])]
                if len(g) < batch_size and max(lens) <= length_ratio * max(1, min(lens)):
                    g.append(i)
                    continue
            groups.append([i])

        self.gpt_batch_last = {
            "mode": mode,
            "rows": int(batch_size),
            "rows_requested": _rows_requested,
            "segments": len(token_id_lists),
            "groups": len(groups),
            "budget_gib": round(_budget / 1024 ** 3, 2) if _budget else 0.0,
            "reason": "clamped" if batch_size < _rows_requested else "",
        }

        out = {}
        total = len(groups)
        # 复刻逐段路径的怪癖：cond_lengths 传的是条件张量的特征维（1024），而不是帧数。
        # 批处理只是把它按行展开，保持与单段完全一致。
        cond_len = spk_cond_emb.shape[-1]
        emo_cond_len = emo_cond_emb.shape[-1]
        for gi, group in enumerate(groups):
            b = len(group)
            max_len = max(len(token_id_lists[i]) for i in group)
            # 右填充 stop_text_token：prepare_gpt_inputs 会逐行剥掉 start/stop token 再按行左填充，
            # 所以填充位不会混进文本 embedding（见 model_v2.py:609-628）。
            text = torch.full((b, max_len), self.gpt.stop_text_token, dtype=torch.int32, device=self.device)
            for row, i in enumerate(group):
                ids = token_id_lists[i]
                text[row, :len(ids)] = torch.as_tensor(ids, dtype=torch.int32, device=self.device)
            cond = spk_cond_emb if spk_cond_emb.shape[0] == b else spk_cond_emb.repeat(b, 1, 1)
            emo_cond = emo_cond_emb if emo_cond_emb.shape[0] == b else emo_cond_emb.repeat(b, 1, 1)
            ev = emovec if emovec.shape[0] == b else emovec.repeat(b, 1)
            if progress is not None:
                progress(gi / total, f"gpt batch {gi + 1}/{total} ({b} segs)")
            _t0 = time.perf_counter()
            with torch.no_grad():
                with torch.amp.autocast(str(self.device).split(":")[0],
                                        enabled=self.dtype is not None, dtype=self.dtype):
                    codes, _ = self.gpt.inference_speech(
                        cond, text, emo_cond,
                        cond_lengths=torch.full((b,), cond_len, dtype=torch.long, device=self.device),
                        emo_cond_lengths=torch.full((b,), emo_cond_len, dtype=torch.long, device=self.device),
                        emo_vec=ev,
                        **gen_params,
                    )
            if os.environ.get("INDEXTTS_DEBUG_BATCH"):
                # CPU 模式下 torch.cuda.max_memory_allocated() 会直接炸，先看设备
                _peak = (torch.cuda.max_memory_allocated() / 1024 ** 3
                         if str(self.device).startswith("cuda") else float("nan"))
                logger.debug(f">> gpt batch {gi + 1}/{total}: rows={b} "
                             f"text_lens={[len(token_id_lists[i]) for i in group]} "
                             f"steps={codes.shape[-1]} {time.perf_counter() - _t0:.2f}s "
                             f"peak={_peak:.2f}GiB")
            for row, i in enumerate(group):
                out[i] = codes[row:row + 1]
        return out

    # 原始推理模式
    def infer(self, spk_audio_prompt, text, output_path,
              emo_audio_prompt=None, emo_alpha=1.0,
              emo_vector=None,
              use_emo_text=False, emo_text=None, use_random=False, interval_silence=200,
              verbose=False, max_text_tokens_per_segment=120, stream_return=False, more_segment_before=0, **generation_kwargs):
        # more_segment_before 是上游 quick_streaming_tokens 的别名：保持形参名
        # 不变以免影响现有调用方，但这里用关键字传入——以前按位置塞进去，
        # 参数名/语义错配，值被静默改作他用。
        if stream_return:
            return self.infer_generator(
                spk_audio_prompt, text, output_path,
                emo_audio_prompt, emo_alpha,
                emo_vector,
                use_emo_text, emo_text, use_random, interval_silence,
                verbose, max_text_tokens_per_segment, stream_return,
                quick_streaming_tokens=more_segment_before, **generation_kwargs
            )
        else:
            try:
                return list(self.infer_generator(
                    spk_audio_prompt, text, output_path,
                    emo_audio_prompt, emo_alpha,
                    emo_vector,
                    use_emo_text, emo_text, use_random, interval_silence,
                    verbose, max_text_tokens_per_segment, stream_return,
                    quick_streaming_tokens=more_segment_before, **generation_kwargs
                ))[0]
            except IndexError:
                print(">> ERROR: inference produced no audio (generator yielded nothing)",
                      file=sys.stderr, flush=True)
                return None

    @torch.no_grad()
    def infer_generator(self, spk_audio_prompt, text, output_path,
              emo_audio_prompt=None, emo_alpha=1.0,
              emo_vector=None,
              use_emo_text=False, emo_text=None, use_random=False, interval_silence=200,
              verbose=False, max_text_tokens_per_segment=120, stream_return=False, quick_streaming_tokens=0, **generation_kwargs):
        print(">> starting inference...")
        self._set_gr_progress(0, "starting inference...")
        if verbose:
            print(f"origin text:{text}, spk_audio_prompt:{spk_audio_prompt}, "
                  f"emo_audio_prompt:{emo_audio_prompt}, emo_alpha:{emo_alpha}, "
                  f"emo_vector:{emo_vector}, use_emo_text:{use_emo_text}, "
                  f"emo_text:{emo_text}")
        start_time = time.perf_counter()

        if use_emo_text or emo_vector is not None:
            # we're using a text or emotion vector guidance; so we must remove
            # "emotion reference voice", to ensure we use correct emotion mixing!
            emo_audio_prompt = None

        if use_emo_text:
            # automatically generate emotion vectors from text prompt
            if emo_text is None:
                emo_text = text  # use main text prompt
            # Qwen 情感分析是一次 LLM 前向（数秒），单独立个阶段上报，
            # 避免这段在 0% 无反馈地卡着
            self._set_gr_progress(0.03, "emotion analysis...|qwen")
            emo_dict = self.qwen_emo.inference(emo_text)
            print(f"detected emotion vectors from text: {emo_dict}")
            # convert ordered dict to list of vectors; the order is VERY important!
            emo_vector = list(emo_dict.values())

        if emo_vector is not None:
            # we have emotion vectors; they can't be blended via alpha mixing
            # in the main inference process later, so we must pre-calculate
            # their new strengths here based on the alpha instead!
            emo_vector_scale = max(0.0, min(1.0, emo_alpha))
            if emo_vector_scale != 1.0:
                # scale each vector and truncate to 4 decimals (for nicer printing)
                emo_vector = [int(x * emo_vector_scale * 10000) / 10000 for x in emo_vector]
                print(f"scaled emotion vectors to {emo_vector_scale}x: {emo_vector}")

        if emo_audio_prompt is None:
            # we are not using any external "emotion reference voice"; use
            # speaker's voice as the main emotion reference audio.
            emo_audio_prompt = spk_audio_prompt
            # must always use alpha=1.0 when we don't have an external reference voice
            emo_alpha = 1.0

        self._set_gr_progress(0.05, "reference audio processing...|ref")

        # 如果参考音频改变了，才需要重新生成, 提升速度
        if self.cache_spk_cond is None or self.cache_spk_audio_prompt != spk_audio_prompt:
            if self.cache_spk_cond is not None:
                self.cache_spk_cond = None
                self.cache_s2mel_style = None
                self.cache_s2mel_prompt = None
                self.cache_mel = None
                torch.cuda.empty_cache()
            audio,sr = self._load_and_cut_audio(spk_audio_prompt,15,verbose)
            audio_22k = torchaudio.transforms.Resample(sr, 22050)(audio)
            audio_16k = torchaudio.transforms.Resample(sr, 16000)(audio)

            inputs = self.extract_features(audio_16k, sampling_rate=16000, return_tensors="pt")
            input_features = inputs["input_features"]
            attention_mask = inputs["attention_mask"]
            input_features = input_features.to(self.device)
            attention_mask = attention_mask.to(self.device)
            spk_cond_emb = self.get_emb(input_features, attention_mask)

            _, S_ref = self.semantic_codec.quantize(spk_cond_emb)
            ref_mel = self.mel_fn(audio_22k.to(spk_cond_emb.device).float())
            ref_target_lengths = torch.LongTensor([ref_mel.size(2)]).to(ref_mel.device)
            feat = torchaudio.compliance.kaldi.fbank(audio_16k.to(ref_mel.device),
                                                     num_mel_bins=80,
                                                     dither=0,
                                                     sample_frequency=16000)
            feat = feat - feat.mean(dim=0, keepdim=True)  # feat2另外一个滤波器能量组特征[922, 80]
            style = self.campplus_model(feat.unsqueeze(0))  # 参考音频的全局style2[1,192]

            with self._autocast_s2mel():
                prompt_condition = self.s2mel.models['length_regulator'](S_ref,
                                                                         ylens=ref_target_lengths,
                                                                         n_quantizers=3,
                                                                         f0=None)[0]

            self.cache_spk_cond = spk_cond_emb
            self.cache_s2mel_style = style
            self.cache_s2mel_prompt = prompt_condition
            self.cache_spk_audio_prompt = spk_audio_prompt
            self.cache_mel = ref_mel
        else:
            style = self.cache_s2mel_style
            prompt_condition = self.cache_s2mel_prompt
            spk_cond_emb = self.cache_spk_cond
            ref_mel = self.cache_mel

        if emo_vector is not None:
            weight_vector = torch.tensor(emo_vector, device=self.device)
            if use_random:
                random_index = [random.randint(0, x - 1) for x in self.emo_num]
            else:
                random_index = [find_most_similar_cosine(style, tmp) for tmp in self.spk_matrix]

            emo_matrix = [tmp[index].unsqueeze(0) for index, tmp in zip(random_index, self.emo_matrix)]
            emo_matrix = torch.cat(emo_matrix, 0)
            emovec_mat = weight_vector.unsqueeze(1) * emo_matrix
            emovec_mat = torch.sum(emovec_mat, 0)
            emovec_mat = emovec_mat.unsqueeze(0)

        if self.cache_emo_cond is None or self.cache_emo_audio_prompt != emo_audio_prompt:
            if self.cache_emo_cond is not None:
                self.cache_emo_cond = None
                torch.cuda.empty_cache()
            emo_audio, _ = self._load_and_cut_audio(emo_audio_prompt,15,verbose,sr=16000)
            emo_inputs = self.extract_features(emo_audio, sampling_rate=16000, return_tensors="pt")
            emo_input_features = emo_inputs["input_features"]
            emo_attention_mask = emo_inputs["attention_mask"]
            emo_input_features = emo_input_features.to(self.device)
            emo_attention_mask = emo_attention_mask.to(self.device)
            emo_cond_emb = self.get_emb(emo_input_features, emo_attention_mask)

            self.cache_emo_cond = emo_cond_emb
            self.cache_emo_audio_prompt = emo_audio_prompt
        else:
            emo_cond_emb = self.cache_emo_cond

        self._set_gr_progress(0.1, "text processing...")
        # 文本规范化可能失败并退回原文（见 front.py normalize 的兜底），
        # 那样读音会不标准。这里把结果透出去，让 server 提示用户，
        # 免得「合成成功但读音不对」变成无声无息。
        self.text_normalize_fallback = None
        text_tokens_list = self.tokenizer.tokenize(text)
        self.text_normalize_fallback = getattr(self.normalizer, "normalize_fallback", None)
        segments = self.tokenizer.split_segments(text_tokens_list, max_text_tokens_per_segment, quick_streaming_tokens = quick_streaming_tokens)
        segments_count = len(segments)

        text_token_ids = self.tokenizer.convert_tokens_to_ids(text_tokens_list)
        if self.tokenizer.unk_token_id in text_token_ids:
            print(f"  >> Warning: input text contains {text_token_ids.count(self.tokenizer.unk_token_id)} unknown tokens (id={self.tokenizer.unk_token_id}):")
            print( "     Tokens which can't be encoded: ", [t for t, id in zip(text_tokens_list, text_token_ids) if id == self.tokenizer.unk_token_id])
            print(f"     Consider updating the BPE model or modifying the text to avoid unknown tokens.")
                  
        if verbose:
            print("text_tokens_list:", text_tokens_list)
            print("segments count:", segments_count)
            print("max_text_tokens_per_segment:", max_text_tokens_per_segment)
            print(*segments, sep="\n")
        do_sample = generation_kwargs.pop("do_sample", True)
        top_p = generation_kwargs.pop("top_p", 0.8)
        top_k = generation_kwargs.pop("top_k", 30)
        temperature = generation_kwargs.pop("temperature", 0.8)
        autoregressive_batch_size = 1
        length_penalty = generation_kwargs.pop("length_penalty", 0.0)
        num_beams = generation_kwargs.pop("num_beams", 3)
        repetition_penalty = generation_kwargs.pop("repetition_penalty", 10.0)
        max_mel_tokens = generation_kwargs.pop("max_mel_tokens", 1500)
        sampling_rate = 22050
        # s2mel CFM 参数：允许通过 generation_kwargs 覆盖实例默认值
        diffusion_steps = generation_kwargs.pop("diffusion_steps", getattr(self, "diffusion_steps", 25))
        inference_cfg_rate = generation_kwargs.pop("inference_cfg_rate", getattr(self, "inference_cfg_rate", 0.7))

        wavs = []
        gpt_gen_time = 0
        gpt_forward_time = 0
        s2mel_time = 0
        bigvgan_time = 0
        has_warned = False
        # 上一次生成是否触到 max_mel_tokens 上限：server 读取后随 done 事件提示前端
        # （文本被静默截断，用户必须知情），下一次生成前复位。
        self.mel_tokens_capped = False
        # 本次生成的段边界快照（字幕用），在拼接前由 _record_segment_slices 填充。
        # 必须在每次生成前清空：否则上一次的字幕会挂到这一次的音频上。
        self.segment_slices = None
        self.segment_text_method = None
        silence = None # for stream_return

        # ---- 段级批处理：非流式路径上先把所有段的 mel codes 预生成 ----
        # b=1 时每个解码步的墙钟时间几乎全花在 CPU 提交上（实测 GPU 只忙 ~4.6ms / 13.8ms），
        # 把多段拼进一个 batch 后每行每 token 从 ~13.8ms 降到 ~5.5ms（b=3 实测）。
        # 流式路径不启用：要等整组生成完才能出首包，首包延迟会变差。
        _gpt_batch_size = int(generation_kwargs.pop("gpt_batch_size", self.gpt_batch_size) or 0)
        _batch_requested = _gpt_batch_size
        precomputed_codes = {}
        # emovec 与 speech_conditioning_latent 只取决于音色/情感参考音频，与文本无关：
        # 原来在每段循环里重算一遍（每段都要过一次条件编码器），这里提到循环外只算一次。
        with torch.no_grad():
            with torch.amp.autocast(str(self.device).split(":")[0],
                                    enabled=self.dtype is not None, dtype=self.dtype):
                emovec = self.gpt.merge_emovec(
                    spk_cond_emb,
                    emo_cond_emb,
                    torch.tensor([spk_cond_emb.shape[-1]], device=self.device),
                    torch.tensor([emo_cond_emb.shape[-1]], device=self.device),
                    alpha=emo_alpha,
                )
                if emo_vector is not None:
                    emovec = emovec_mat + (1 - torch.sum(weight_vector)) * emovec
                speech_conditioning_latent = self.gpt.get_conditioning(
                    spk_cond_emb.transpose(1, 2),
                    torch.tensor([spk_cond_emb.shape[-1]], device=self.device),
                )
        _gen_params = dict(
            do_sample=do_sample, top_p=top_p, top_k=top_k, temperature=temperature,
            num_return_sequences=autoregressive_batch_size, length_penalty=length_penalty,
            num_beams=num_beams, repetition_penalty=repetition_penalty,
            max_generate_length=max_mel_tokens, **generation_kwargs,
        )
        if not stream_return and segments_count > 1 and _gpt_batch_size != 0 and _gpt_batch_size != 1:
            # 先把上一轮生成留下的缓存块还给驱动，再读空闲显存。Windows 的 torch 分配器
            # 不清空的话，同一次对话里第二次生成会看到假低的 free，自动模式直接掉成 1 行。
            try:
                torch.cuda.empty_cache()
            except Exception as e:
                if os.environ.get("INDEXTTS_DEBUG_BATCH"):
                    logger.debug(f">> empty_cache failed ({e})")
        if _gpt_batch_size < 0:
            # 负值 = 自动：按本机真实空闲显存决定拼几段，小到 6G、大到 24G 的卡各自吃到自己的上限
            _gpt_batch_size = self._auto_gpt_batch_size(_gen_params)
            if os.environ.get("INDEXTTS_DEBUG_BATCH"):
                logger.debug(f">> gpt batch auto rows={_gpt_batch_size}")
        if _gpt_batch_size > 1 and not stream_return and segments_count > 1:
            _batch_start = time.perf_counter()
            precomputed_codes = self._precompute_segment_codes(
                spk_cond_emb, emo_cond_emb, emovec,
                [self.tokenizer.convert_tokens_to_ids(sent) for sent in segments],
                _gen_params, batch_size=_gpt_batch_size,
                progress=lambda frac, desc: self._set_gr_progress(0.1 + 0.4 * frac, desc),
                mode="auto" if _batch_requested < 0 else "manual",
            )
            gpt_gen_time += time.perf_counter() - _batch_start
            _prog_lo = 0.5
        else:
            # 没拼批时记清原因，前端「本机实际档位」那一行才能解释清楚：
            # 是流式、只有一段，还是自动模式算出来显存不够。
            if stream_return:
                _batch_off_reason = "streaming"
            elif segments_count <= 1:
                _batch_off_reason = "single_segment"
            elif _gpt_batch_size <= 1 and _batch_requested < 0:
                _batch_off_reason = "low_vram"
            else:
                _batch_off_reason = "disabled"
            self.gpt_batch_last = {
                "mode": "auto" if _batch_requested < 0 else ("manual" if _batch_requested > 1 else "off"),
                "rows": int(_gpt_batch_size) if _gpt_batch_size > 1 else 0,
                "rows_requested": int(_batch_requested),
                "segments": int(segments_count),
                "groups": 0,
                "budget_gib": 0.0,
                "reason": _batch_off_reason,
            }
            _prog_lo = 0.2

        for seg_idx, sent in enumerate(segments):
            seg_lo = _prog_lo + (0.9 - _prog_lo) * seg_idx / segments_count
            seg_hi = _prog_lo + (0.9 - _prog_lo) * (seg_idx + 1) / segments_count
            seg_span = seg_hi - seg_lo
            seg_desc = f"speech synthesis {seg_idx + 1}/{segments_count}..."
            self._set_gr_progress(seg_lo, seg_desc + "|gpt")

            text_tokens = self.tokenizer.convert_tokens_to_ids(sent)
            text_tokens = torch.tensor(text_tokens, dtype=torch.int32, device=self.device).unsqueeze(0)
            if verbose:
                print(text_tokens)
                print(f"text_tokens shape: {text_tokens.shape}, text_tokens type: {text_tokens.dtype}")
                # debug tokenizer
                text_token_syms = self.tokenizer.convert_ids_to_tokens(text_tokens[0].tolist())
                print("text_token_syms is same as segment tokens", text_token_syms == sent)

            m_start_time = time.perf_counter()
            if seg_idx in precomputed_codes:
                # 段级批处理路径：codes 已在预生成阶段算好，这里只取用结果，
                # 下游（截断检查 / s2mel / bigvgan / 流式）与逐段路径完全共用。
                codes = precomputed_codes[seg_idx]
                self._set_gr_progress(seg_lo + seg_span * self._phase_w()[0], seg_desc + "|gpt")
            else:
                with torch.no_grad():
                    with torch.amp.autocast(text_tokens.device.type, enabled=self.dtype is not None, dtype=self.dtype):
                        est_gpt_steps = max(1, min(max_mel_tokens, round(text_tokens.shape[1] * 12)))

                        class _MelTokenProgress(StoppingCriteria):
                            def __init__(s, cb):
                                s.cb = cb
                                s.n = 0
                            def __call__(s, input_ids, scores, **kw):
                                s.n += 1
                                if s.n % 10 == 0:
                                    s.cb(min(1.0, s.n / est_gpt_steps))
                                return False

                        gpt_hook = _MelTokenProgress(
                            lambda f: self._set_gr_progress(seg_lo + seg_span * self._phase_w()[0] * f, seg_desc + "|gpt")
                        )

                        codes, speech_conditioning_latent = self.gpt.inference_speech(
                            spk_cond_emb,
                            text_tokens,
                            emo_cond_emb,
                            cond_lengths=torch.tensor([spk_cond_emb.shape[-1]], device=text_tokens.device),
                            emo_cond_lengths=torch.tensor([emo_cond_emb.shape[-1]], device=text_tokens.device),
                            emo_vec=emovec,
                            do_sample=do_sample,
                            top_p=top_p,
                            top_k=top_k,
                            temperature=temperature,
                            num_return_sequences=autoregressive_batch_size,
                            length_penalty=length_penalty,
                            num_beams=num_beams,
                            repetition_penalty=repetition_penalty,
                            max_generate_length=max_mel_tokens,
                            stopping_criteria=StoppingCriteriaList([gpt_hook]),
                            **generation_kwargs
                        )

            gpt_gen_time += time.perf_counter() - m_start_time
            if not has_warned and (codes[:, -1] != self.stop_mel_token).any():
                warnings.warn(
                    f"WARN: generation stopped due to exceeding `max_mel_tokens` ({max_mel_tokens}). "
                    f"Input text tokens: {text_tokens.shape[1]}. "
                    f"Consider reducing `max_text_tokens_per_segment`({max_text_tokens_per_segment}) or increasing `max_mel_tokens`.",
                    category=RuntimeWarning
                )
                has_warned = True
                self.mel_tokens_capped = True

            code_lens = []
            max_code_len = 0
            for code in codes:
                if self.stop_mel_token not in code:
                    code_len = len(code)
                else:
                    len_ = (code == self.stop_mel_token).nonzero(as_tuple=False)[0]
                    code_len = len_[0].item() if len_.numel() > 0 else len(code)
                code_lens.append(code_len)
                max_code_len = max(max_code_len, code_len)
            codes = codes[:, :max_code_len]
            code_lens = torch.LongTensor(code_lens)
            code_lens = code_lens.to(self.device)
            if verbose:
                print(codes, type(codes))
                print(f"fix codes shape: {codes.shape}, codes type: {codes.dtype}")
                print(f"code len: {code_lens}")

            m_start_time = time.perf_counter()
            use_speed = torch.zeros(spk_cond_emb.size(0)).to(spk_cond_emb.device).long()
            with torch.amp.autocast(text_tokens.device.type, enabled=self.dtype is not None, dtype=self.dtype):
                latent = self.gpt(
                    speech_conditioning_latent,
                    text_tokens,
                    torch.tensor([text_tokens.shape[-1]], device=text_tokens.device),
                    codes,
                    torch.tensor([codes.shape[-1]], device=text_tokens.device),
                    emo_cond_emb,
                    cond_mel_lengths=torch.tensor([spk_cond_emb.shape[-1]], device=text_tokens.device),
                    emo_cond_mel_lengths=torch.tensor([emo_cond_emb.shape[-1]], device=text_tokens.device),
                    emo_vec=emovec,
                    use_speed=use_speed,
                )
                gpt_forward_time += time.perf_counter() - m_start_time
                self._set_gr_progress(seg_lo + seg_span * (self._phase_w()[0] + self._phase_w()[1]), seg_desc + "|s2mel")

            with self._autocast_s2mel():
                m_start_time = time.perf_counter()
                latent = self.s2mel.models['gpt_layer'](latent)
                S_infer = self.semantic_codec.quantizer.vq2emb(codes.unsqueeze(1))
                S_infer = S_infer.transpose(1, 2)
                S_infer = S_infer + latent
                target_lengths = (code_lens * 1.72).long()

                cond = self.s2mel.models['length_regulator'](S_infer,
                                                             ylens=target_lengths,
                                                             n_quantizers=3,
                                                             f0=None)[0]
                cat_condition = torch.cat([prompt_condition, cond], dim=1)
                # s2mel 扩散阶段实时进度：把段内 s2mel 权重区间 (w0+w1, w0+w1+w2) 细分上报，
                # desc 带 |s2mel|N/M 扩散步数（旧前端 split("|")[1] 仍取到 "s2mel"，向后兼容）
                _w = self._phase_w()
                _s2mel_base = seg_lo + seg_span * (_w[0] + _w[1])
                _s2mel_span = seg_span * _w[2]
                vc_target = self.s2mel.models['cfm'].inference(cat_condition,
                                                               torch.LongTensor([cat_condition.size(1)]).to(
                                                                   cond.device),
                                                               ref_mel, style, None, diffusion_steps,
                                                               inference_cfg_rate=inference_cfg_rate,
                                                               progress_cb=lambda f: self._set_gr_progress(
                                                                   _s2mel_base + _s2mel_span * f,
                                                                   seg_desc + f"|s2mel|{int(round(f * diffusion_steps))}/{diffusion_steps}"))
                vc_target = vc_target[:, :, ref_mel.size(-1):]
                s2mel_time += time.perf_counter() - m_start_time
                self._set_gr_progress(seg_lo + seg_span * (self._phase_w()[0] + self._phase_w()[1] + self._phase_w()[2]), seg_desc + "|bigvgan")

            m_start_time = time.perf_counter()
            with torch.no_grad():
                wav = self.bigvgan(vc_target.float()).squeeze().unsqueeze(0)
            bigvgan_time += time.perf_counter() - m_start_time
            self._set_gr_progress(seg_hi, seg_desc + "|bigvgan")
            wav = wav.squeeze(1)

            wav = torch.clamp(32767 * wav, -32767.0, 32767.0)
            if verbose:
                print(f"wav shape: {wav.shape}", "min:", wav.min(), "max:", wav.max())
            # wavs.append(wav[:, :-512])
            wavs.append(wav.cpu())  # to cpu before saving
            if stream_return:
                yield wav.cpu()
                if silence is None:
                    silence = self.interval_silence(wavs, sampling_rate=sampling_rate, interval_silence=interval_silence)
                yield silence
        end_time = time.perf_counter()

        # 段边界要在插入段间静音【之前】记录：insert_interval_silence 会返回把静音
        # 张量也塞进去的新列表，那时 len 和样本数都不是「每段音频」的口径了。
        self._record_segment_slices(wavs, segments, text, sampling_rate, interval_silence)

        wavs = self.insert_interval_silence(wavs, sampling_rate=sampling_rate, interval_silence=interval_silence)
        if not wavs:
            # 调试细节只进内部日志；用户只需知道「为什么没有音频」
            if os.environ.get("INDEXTTS_DEBUG_BATCH"):
                logger.debug(
                    f">> EMPTY wavs debug: segments_count={segments_count} "
                    f"precomputed={sorted(precomputed_codes)} prog_lo={_prog_lo} segs={segments!r}"
                )
            raise RuntimeError("合成未产生任何音频段：输入文本可能为空或被全部过滤")
        wav = torch.cat(wavs, dim=1)
        wav_length = wav.shape[-1] / sampling_rate
        _report(f">> gpt_gen_time: {gpt_gen_time:.2f} seconds")
        _report(f">> gpt_forward_time: {gpt_forward_time:.2f} seconds")
        _report(f">> s2mel_time: {s2mel_time:.2f} seconds")
        _report(f">> bigvgan_time: {bigvgan_time:.2f} seconds")
        phase_tot = gpt_gen_time + gpt_forward_time + s2mel_time + bigvgan_time
        if phase_tot > 0:
            cur = (gpt_gen_time / phase_tot, gpt_forward_time / phase_tot,
                   s2mel_time / phase_tot, bigvgan_time / phase_tot)
            if self._phase_ema is None:
                self._phase_ema = list(cur)
            else:
                self._phase_ema = [0.7 * a + 0.3 * b for a, b in zip(self._phase_ema, cur)]
        _report(f">> Total inference time: {end_time - start_time:.2f} seconds")
        _report(f">> Generated audio length: {wav_length:.2f} seconds")
        _report(f">> RTF: {(end_time - start_time) / wav_length:.4f}")

        # save audio
        wav = wav.cpu()  # to cpu
        if output_path:
            # 直接保存音频到指定路径中
            if os.path.isfile(output_path):
                os.remove(output_path)
                _report(f">> remove old wav file: {output_path}")
            if os.path.dirname(output_path) != "":
                os.makedirs(os.path.dirname(output_path), exist_ok=True)
            torchaudio.save(output_path, wav.type(torch.int16), sampling_rate)
            _report(f">> wav file saved to: {output_path}")
            if stream_return:
                return None
            yield output_path
        else:
            if stream_return:
                return None
            # 返回以符合Gradio的格式要求
            wav_data = wav.type(torch.int16)
            wav_data = wav_data.numpy().T
            yield (sampling_rate, wav_data)


def find_most_similar_cosine(query_vector, matrix):
    query_vector = query_vector.float()
    matrix = matrix.float()

    similarities = F.cosine_similarity(query_vector, matrix, dim=1)
    most_similar_index = torch.argmax(similarities)
    return most_similar_index

_EMO_PAIR_RE = re.compile(r'([^\s":.,]+?)"?\s*:\s*(\d+(?:\.\d+)?)')


def _parse_emo_dict(raw):
    """把 QwenEmotion 的原始输出解析成 {中文情绪: 分数}。

    json.loads 成功但结果不是 dict（模型偶尔吐数组或裸数字）时同样要兜底，
    否则后面的 content.get() / content[k] 会抛 AttributeError / TypeError。
    """
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        data = None
    if isinstance(data, dict):
        return data
    parsed = {}
    for m in _EMO_PAIR_RE.finditer(raw if isinstance(raw, str) else ""):
        try:
            parsed[m.group(1)] = float(m.group(2))
        except ValueError:
            continue
    return parsed


class QwenEmotion:
    def __init__(self, model_dir, use_fp16=True):
        self.model_dir = model_dir
        self.tokenizer = AutoTokenizer.from_pretrained(self.model_dir)
        self.model = AutoModelForCausalLM.from_pretrained(
            self.model_dir,
            torch_dtype="float16" if use_fp16 else "float32",  # "auto"
            device_map="auto"
        )
        self.prompt = "文本情感分类"
        self.cn_key_to_en = {
            "高兴": "happy",
            "愤怒": "angry",
            "悲伤": "sad",
            "恐惧": "afraid",
            "反感": "disgusted",
            # TODO: the "低落" (melancholic) emotion will always be mapped to
            # "悲伤" (sad) by QwenEmotion's text analysis. it doesn't know the
            # difference between those emotions even if user writes exact words.
            # SEE: `self.melancholic_words` for current workaround.
            "低落": "melancholic",
            "惊讶": "surprised",
            "自然": "calm",
        }
        self.desired_vector_order = ["高兴", "愤怒", "悲伤", "恐惧", "反感", "低落", "惊讶", "自然"]
        self.melancholic_words = {
            # emotion text phrases that will force QwenEmotion's "悲伤" (sad) detection
            # to become "低落" (melancholic) instead, to fix limitations mentioned above.
            "低落",
            "melancholy",
            "melancholic",
            "depression",
            "depressed",
            "gloomy",
        }
        self.max_score = 1.2
        self.min_score = 0.0

    def clamp_score(self, value):
        try:
            value = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"QwenEmotion returned a non-numeric emotion score {value!r}. "
                "Please retry the request."
            ) from exc
        return max(self.min_score, min(self.max_score, value))

    def convert(self, content):
        # generate emotion vector dictionary:
        # - insert values in desired order (Python 3.7+ `dict` remembers insertion order)
        # - convert Chinese keys to English
        # - clamp all values to the allowed min/max range
        # - use 0.0 for any values that were missing in `content`
        emotion_dict = {
            self.cn_key_to_en[cn_key]: self.clamp_score(content.get(cn_key, 0.0))
            for cn_key in self.desired_vector_order
        }

        # default to a calm/neutral voice if all emotion vectors were empty
        if all(val <= 0.0 for val in emotion_dict.values()):
            _report(">> no emotions detected; using default calm/neutral voice")
            emotion_dict["calm"] = 1.0

        return emotion_dict

    def inference(self, text_input):
        start = time.time()
        messages = [
            {"role": "system", "content": f"{self.prompt}"},
            {"role": "user", "content": f"{text_input}"}
        ]
        text = self.tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=False,
        )
        model_inputs = self.tokenizer([text], return_tensors="pt").to(self.model.device)

        # conduct text completion
        generated_ids = self.model.generate(
            **model_inputs,
            max_new_tokens=32768,
            pad_token_id=self.tokenizer.eos_token_id
        )
        output_ids = generated_ids[0][len(model_inputs.input_ids[0]):].tolist()

        # parsing thinking content
        try:
            # rindex finding 151668 (</think>)
            index = len(output_ids) - output_ids[::-1].index(151668)
        except ValueError:
            index = 0

        raw = self.tokenizer.decode(output_ids[index:], skip_special_tokens=True)
        content = _parse_emo_dict(raw)

        # workaround for QwenEmotion's inability to distinguish "悲伤" (sad) vs "低落" (melancholic).
        # if we detect any of the IndexTTS "melancholic" words, we swap those vectors
        # to encode the "sad" emotion as "melancholic" (instead of sadness).
        text_input_lower = text_input.lower()
        if any(word in text_input_lower for word in self.melancholic_words):
            # print(">> before vec swap", content)
            content["悲伤"], content["低落"] = content.get("低落", 0.0), content.get("悲伤", 0.0)
            # print(">>  after vec swap", content)

        return self.convert(content)


if __name__ == "__main__":
    prompt_wav = "examples/voice_01.wav"
    text = '欢迎大家来体验indextts2，并给予我们意见与反馈，谢谢大家。'
    tts = IndexTTS2(
        cfg_path="checkpoints/config.yaml", 
        model_dir="checkpoints", 
    )
    tts.infer(spk_audio_prompt=prompt_wav, text=text, output_path="gen.wav", verbose=True)
    char_size = 5
    import string
    time_buckets = []
    for i in range(10):
        text = ''.join(random.choices(string.ascii_letters, k=char_size))
        start_time = time.time()
        tts.infer(spk_audio_prompt=prompt_wav, text=text, output_path="gen.wav", verbose=True)
        time_buckets.append(time.time() - start_time)
    print(time_buckets)
