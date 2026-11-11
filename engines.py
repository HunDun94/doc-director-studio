"""
文档导演台 · 引擎层（仅本地 ComfyUI）

云端引擎（可灵 / 即梦）已移除：不需要 API Key，不产生费用，全部走本机 ComfyUI。
这个文件保留同名接口，是为了 doc_director.py 里的调用不用改。
"""

CLOUD_ENGINES = tuple()          # 没有云端引擎了

LIMITS = {
    "comfy": {
        "min_sec": 1, "max_sec": 20,
        "max_images": 9, "min_img_px": 0,
        "ratio_range": (0.2, 5.0),
        "durations": "1~20 秒（受显存限制）",
    },
}

_LABELS = {"comfy": "本地 ComfyUI"}


def engine_label(engine):
    return _LABELS.get(engine, engine)


def estimate_usd(engine, total_sec, mode="pro", sound=False):
    """本地生成不花钱。"""
    return 0.0


def check_shot(engine, shot, pic_paths):
    """本地没有云端那套时长/张数硬限制，一律放行。"""
    return []


def run(engine, shot, pic_paths, cfg, outdir, log=print, stop_check=None, ffmpeg_bin=None):
    raise RuntimeError("云端引擎已移除，现在只能用本地 ComfyUI 生成")
