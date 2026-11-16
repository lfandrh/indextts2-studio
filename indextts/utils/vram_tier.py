"""按显存自动分档（VRAM Tier）的纯策略模块。

刻意不依赖 ``indextts.infer_v2`` 的重推理栈：阈值与策略表可以在无 GPU、
没装 transformers / modelscope 的环境里被单测直接导入验证。

**重要语义**：分档只决定「辅助模型用完退不退、批行上限、切段上限」。
它**不改变任何模型的推理设备**（推理恒在 ``IndexTTS2.device``），也**不改变
精度 / 扩散步数 / CFG / 批处理算法**。所谓「offload 到 CPU」只指 w2v-bert
的**权重存放位置**，绝不指把 w2v 放到 CPU 上做前向——参考项目 2.5 的 4g
CPU 档就是因为这个混淆而自相矛盾（``infer_v2_5.py:399`` 又把模型搬回 GPU）。
"""

from __future__ import annotations

from typing import Dict, Optional

TIER_CHOICES = ("auto", "4g", "balanced", "turbo")

_AUTO_LOW_GB = 6.0
_AUTO_HIGH_GB = 11.5

# max_* 为 None 表示该档不额外夹取（沿用主项目默认行为）。
TIER_POLICY: Dict[str, Dict[str, object]] = {
    "4g": {
        "w2v_offload_after_use": True,
        "offload_deep": True,          # 用完彻底释放，下次 miss 重新 from_pretrained
        "max_gpt_batch_rows": 1,
        "max_text_tokens_per_segment": 60,
    },
    "balanced": {
        "w2v_offload_after_use": True,
        "offload_deep": False,         # 权重搬到 CPU 内存留着，下次 .to(device) 秒回
        "max_gpt_batch_rows": 3,
        "max_text_tokens_per_segment": 80,
    },
    "turbo": {
        "w2v_offload_after_use": False,
        "offload_deep": False,
        "max_gpt_batch_rows": None,
        "max_text_tokens_per_segment": None,
    },
}


def normalize_tier(requested) -> str:
    """非法/缺失值一律收敛成 ``auto``，避免配置里一个错字让服务 500。"""
    if isinstance(requested, str) and requested.strip().lower() in TIER_CHOICES:
        return requested.strip().lower()
    return "auto"


def detect_total_vram_gb(device_index: int = 0) -> float:
    """总显存（GiB）。无 CUDA / 探测失败 / 非 GPU 机器一律返回 0.0。

    只看**总**显存而不是空闲显存：分档是稳定的启动期决策，不能因为别的
    程序临时占显存就在两次请求之间跳档。运行时的空闲显存只在生成时用来
    清缓存与压批行，不改变 effective_tier。
    """
    try:
        import torch
        if not torch.cuda.is_available():
            return 0.0
        return round(float(torch.cuda.get_device_properties(device_index).total_memory)
                     / (1024 ** 3), 2)
    except Exception:
        return 0.0


def auto_tier(detected_vram_gb: float) -> str:
    if detected_vram_gb <= 0:
        # 无 GPU / 探测不到：与 8G 卡同档，策略上「用完退」但没有显存可退
        return "balanced"
    if detected_vram_gb < _AUTO_LOW_GB:
        return "4g"
    if detected_vram_gb < _AUTO_HIGH_GB:
        return "balanced"
    return "turbo"


def resolve_vram_tier(requested="auto", detected_vram_gb: Optional[float] = None) -> Dict[str, object]:
    """返回 ``{requested, effective, detected_vram_gb, <策略字段...>}``。"""
    if detected_vram_gb is None:
        detected_vram_gb = detect_total_vram_gb()
    requested = normalize_tier(requested)
    effective = auto_tier(float(detected_vram_gb)) if requested == "auto" else requested
    return {
        "requested": requested,
        "effective": effective,
        "detected_vram_gb": float(detected_vram_gb),
        **TIER_POLICY[effective],
    }
