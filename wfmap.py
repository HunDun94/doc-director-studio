# -*- coding: utf-8 -*-
"""
wfmap.py · 通用 ComfyUI 工作流映射
--------------------------------------------------
目标：不写死节点 ID，任何「文字 + 图 → 视频」的 API 工作流都能用。

做法：
  1. detect(g)  自动从工作流里猜出「提示词节点 / 时长节点 / 种子节点 /
                图槽(LoadImage) / 输出前缀节点」
  2. apply(g, ..) 按映射把值注进去，工作流的连线一律不动

映射结构（存 workflow_map.json）：
{
  "template_hash": "…",          # 模板变了就提示重新探测
  "prompt":   {"node": "5386", "input": "value"},
  "duration": {"node": "132",  "input": "value", "unit": "seconds", "int": false},
  "seed":     {"node": "5417", "input": "seed"},
  "images":   [{"node": "264"}, {"node": "564"}, {"node": "269"}],
  "outputs":  [{"node": "665"}, {"node": "667"}],
  "extra":    [{"node": "5142", "input": "mode", "value": "…"}],
  "fps": 24
}
"""
import hashlib
import json
import re

# 这些 input 名基本可以断定是「时长 / 帧数」
DUR_KEYS = ("length", "duration", "seconds", "sec", "video_length",
            "total_frames", "num_frames", "frames", "frame_count", "max_frames")
FRAME_KEYS = ("total_frames", "num_frames", "frames", "frame_count", "max_frames")
SEED_KEYS = ("seed", "noise_seed", "rand_seed")
IMG_CLASSES = ("LoadImage", "LoadImageMask", "LoadImageFromPath", "ImageLoad",
               "LoadImageOutput")
VIDEO_CLASSES = ("VHS_LoadVideo", "VHS_LoadVideoPath", "VHS_LoadVideoFFmpeg",
                 "LoadVideo", "LoadVideoUpload", "LoadVideoPath")
VIDEO_KEY_HINT = ("video", "file", "video_path", "filename", "path", "url")
TEXT_CLASSES = ("CLIPTextEncode", "CLIPTextEncodeSDXL", "TextEncodeQwenImageEdit",
                "String Literal", "easy positive", "easy negative")


def _isnum(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _idkey(nid):
    try:
        return (0, int(nid))
    except Exception:
        return (1, str(nid))


REF_RE_CACHE = {}


def _trace_image(g, nid, depth=0):
    """从一个节点往上找它最终连到的 LoadImage。"""
    if depth > 5 or nid not in g:
        return None
    if (g[nid].get("class_type") or "") in ("LoadImage", "LoadImageMask",
                                            "LoadImageFromPath", "LoadImageOutput"):
        return nid
    for k, v in (g[nid].get("inputs") or {}).items():
        if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str):
            r = _trace_image(g, v[0], depth + 1)
            if r:
                return r
    return None


def _ordered_images(g, all_images):
    """
    图槽顺序：优先按 ref_image_N / imageN 这类「带序号的参考图输入」的连线顺序
    （ref_image_0 = 图1，ref_image_1 = 图2 ……），这样和图1/图2/图3 语义对齐。
    找不到再按节点 ID 排。
    """
    import re
    refs = {}
    for nid, node in g.items():
        for k, v in (node.get("inputs") or {}).items():
            m = re.search(r"(?:^|\.)ref_image_(\d+)$", k) or re.search(r"^(?:image|img|ref_image)(\d+)$", k)
            if not m:
                continue
            if not (isinstance(v, list) and len(v) == 2 and isinstance(v[0], str)):
                continue
            idx = int(m.group(1))
            src = _trace_image(g, v[0])
            if src and idx not in refs:
                refs[idx] = src
    ordered = [refs[i] for i in sorted(refs) if refs[i] in all_images]
    for n in all_images:
        if n not in ordered:
            ordered.append(n)
    return ordered


def template_hash(g):
    s = json.dumps(g, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:12]


def _unit_of(ct, k):
    """判断这个时长参数的单位。"""
    lk = k.lower()
    if lk in FRAME_KEYS:
        return "frames"
    if lk == "length":
        # Primitive 节点是用户手填的秒数；其他节点（Wan/CogVideo/AnimateDiff）的 length 基本是帧数
        return "seconds" if ct.startswith("Primitive") else "frames"
    return "seconds"


def _title(g, titles, nid):
    """节点显示名：用户导入的标题 > _meta.title > 空"""
    t = (titles or {}).get(str(nid))
    if not t:
        t = ((g.get(nid) or {}).get("_meta") or {}).get("title") or ""
    return t.strip()


def _title_num(t):
    """从「图3」「Image2」「image_1」这类标题里取出序号。"""
    if not t:
        return None
    m = re.search(r"(?:图|image|img|ref)[\s_\-]*([1-9])", t, re.I)
    return int(m.group(1)) if m else None


def _video_key(g, nid):
    """视频加载节点里哪个 input 是文件名字段。"""
    ins = (g.get(nid) or {}).get("inputs") or {}
    for hint in VIDEO_KEY_HINT:
        for k, v in ins.items():
            if k.lower() == hint and isinstance(v, str):
                return k
    for k, v in ins.items():
        if isinstance(v, str):
            return k
    return "video"


def detect(g, titles=None):
    """自动探测映射。titles: {节点id: 显示名}，用来优先认「种子」「图1」这类中文标题。"""
    images, videos, outputs, seeds, durs, texts = [], [], [], [], [], []

    for nid, node in g.items():
        ct = (node.get("class_type") or "")
        ins = node.get("inputs") or {}
        ti = _title(g, titles, nid)

        # ---- 图槽 / 视频槽 ----
        if ct in IMG_CLASSES:
            images.append(nid)
        if ct in VIDEO_CLASSES:
            videos.append(nid)

        # ---- 输出前缀 ----
        if "filename_prefix" in ins:
            outputs.append(nid)

        # ---- 逐个 input 判断 ----
        for k, v in ins.items():
            lk = k.lower()
            if lk in SEED_KEYS and _isnum(v):
                seeds.append((nid, k))
            if lk in DUR_KEYS and _isnum(v):
                durs.append((nid, k))
            if lk == "value" and _isnum(v) and ct.startswith("Primitive"):
                durs.append((nid, k))

        # ---- 提示词候选 ----
        if ct.startswith("PrimitiveString"):
            v = ins.get("value")
            if isinstance(v, str):
                pri = 0 if "Multiline" in ct else 1
                # 标题明确写了提示词 / 负面词，直接定性
                if ti and re.search(r"提示词|prompt|正向|positive", ti, re.I):
                    pri = -3
                if ti and re.search(r"负面|反向|negative", ti, re.I):
                    pri = 9
                texts.append((pri, -len(v), nid, "value", len(v)))
        elif ct in TEXT_CLASSES:
            v = ins.get("text")
            if isinstance(v, str):
                pri = 2
                if ti and re.search(r"负面|反向|negative", ti, re.I):
                    pri = 9
                elif ti and re.search(r"提示词|prompt|正向|positive", ti, re.I):
                    pri = -3
                texts.append((pri, -len(v), nid, "text", len(v)))

    # ---- 排序 ----
    # 图槽：如果每个节点都起过「图1/图2/图3」这种名字，就按名字排；否则退回连线顺序
    titled = [(_title_num(_title(g, titles, n)), n) for n in images]
    if images and all(t[0] for t in titled):
        titled.sort()
        images = [t[1] for t in titled]
    else:
        images.sort(key=_idkey)
        images = _ordered_images(g, images)
    outputs.sort(key=_idkey)

    def seed_key(c):
        ct = (g[c[0]].get("class_type") or "")
        ti = _title(g, titles, c[0])
        named = -1 if re.search(r"种子|seed", ti, re.I) else 0
        return (named,
                0 if "Seed" in ct else 1,           # 专用种子节点优先，采样器自带的 seed 靠后
                0 if c[1].lower() == "seed" else 1,
                _idkey(c[0]))
    seeds.sort(key=seed_key)

    def dur_score(c):
        nid, k = c
        ct = (g[nid].get("class_type") or "")
        ti = _title(g, titles, nid)
        lk = k.lower()
        s = 0
        if re.search(r"时长|duration|秒数|seconds", ti, re.I):
            s -= 50        # 用户起名「视频时长（s）」，最可信
        if ct.startswith("PrimitiveFloat"):
            s -= 30        # 直接的浮点时长，最可信
        elif ct.startswith("PrimitiveInt"):
            s -= 26
        elif ct.startswith("Primitive"):
            s -= 20
        if lk in ("length", "duration", "video_length", "seconds", "sec"):
            s -= 12
        if "Temporal" in ct or "Length" in ct or "Duration" in ct:
            s -= 6         # 分块参数，优先级低于纯时长
        if lk in FRAME_KEYS:
            s -= 2
        return (s, _idkey(nid))

    durs.sort(key=dur_score)
    texts.sort()

    dur = None
    if durs:
        nid, k = durs[0]
        ct = (g[nid].get("class_type") or "")
        unit = _unit_of(ct, k)
        dur = {
            "node": nid, "input": k, "unit": unit,
            "int": unit == "frames" or ct.startswith("PrimitiveInt"),
        }

    seed = None
    if seeds:
        seed = {"node": seeds[0][0], "input": seeds[0][1]}

    prompt = None
    if texts:
        _, _, nid, key, ln = texts[0]
        prompt = {"node": nid, "input": key}

    return {
        "prompt": prompt,
        "duration": dur,
        "seed": seed,
        "images": [{"node": n} for n in images],
        "all_images": images,           # 供 UI 下拉选
        "videos": [{"node": n, "input": _video_key(g, n)} for n in videos],
        "all_videos": videos,
        "outputs": [{"node": n} for n in outputs],
        "fps": 24,
    }


def clean(m):
    """去掉 UI 用的辅助字段，得到可存盘的映射。"""
    if not m:
        return {}
    return {
        "prompt": m.get("prompt"),
        "duration": m.get("duration"),
        "seed": m.get("seed"),
        "images": m.get("images") or [],
        "videos": m.get("videos") or [],
        "outputs": m.get("outputs") or [],
        "extra": m.get("extra") or [],
        "extra_texts": m.get("extra_texts") or [],
        "fps": m.get("fps") or 24,
        "template_hash": m.get("template_hash") or "",
    }


def _prune_refs(g, keep_nodes):
    """
    没填图的参考槽，把连线从参考节点上摘掉。
    否则模板里原本连着的默认图会被一起算进去，参考图越多越吃显存。
    """
    for nid, node in list(g.items()):
        ins = node.get("inputs") or {}
        for k, v in list(ins.items()):
            if "ref_image_" not in str(k).lower():
                continue
            if not (isinstance(v, list) and len(v) == 2 and isinstance(v[0], str)):
                continue
            src = _trace_image(g, v[0])
            if src and src not in keep_nodes:
                ins.pop(k, None)
    return g


def _drop_orphan_images(g):
    """
    参考图断开后，把那些已经没有任何下游的 LoadImage 直接从工作流里删掉，
    这样提交给 ComfyUI 的任务里就只剩这一段真正用到的图。
    """
    used = set()
    for node in g.values():
        for v in (node.get("inputs") or {}).values():
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str):
                used.add(v[0])
    for nid in list(g.keys()):
        n = g.get(nid) or {}
        if (n.get("class_type") or "") in ("LoadImage", "LoadImageMask", "LoadImageOutput"):
            if nid not in used:
                g.pop(nid, None)
    return g


def prune_graph(g):
    """
    从「没有任何下游的节点」当根，反向把依赖链全收进来，其余节点一律删掉。
    这样没用到的参考图连同它后面的 Resize 节点都不会提交给 ComfyUI，显存和耗时都省。
    """
    dep = {}
    for nid, node in g.items():
        deps = set()
        for v in (node.get("inputs") or {}).values():
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str):
                deps.add(v[0])
        dep[nid] = deps

    referenced = set()
    for deps in dep.values():
        referenced |= deps

    # 根=真正的输出节点（保存/预览类）。找不到才退回「没有下游的节点」
    roots = [n for n in g
             if "Save" in (g[n].get("class_type") or "")
             or "Preview" in (g[n].get("class_type") or "")
             or (g[n].get("class_type") or "") == "VHS_VideoCombine"]
    if not roots:
        roots = [n for n in g if n not in referenced]

    keep = set()
    stack = list(roots)
    while stack:
        n = stack.pop()
        if n in keep or n not in g:
            continue
        keep.add(n)
        for d in dep.get(n, ()):
            if d in g and d not in keep:
                stack.append(d)

    for n in list(g.keys()):
        if n not in keep:
            g.pop(n, None)
    return g


def apply(g, shot, mapping, pic_names, vid_names=None):
    """
    把一段的值注入工作流副本。
    pic_names: {1: "sub/xxx.png", 2: ...}
    vid_names: {1: "sub/xxx.mp4", ...}
    """
    vid_names = vid_names or {}
    prompt_id = shot.get("prompt", "")

    p = mapping.get("prompt")
    if p and p.get("node") in g:
        g[p["node"]]["inputs"][p.get("input") or "value"] = prompt_id

    d = mapping.get("duration")
    if d and d.get("node") in g:
        sec = float(shot.get("sec") or 5)
        fps = float(mapping.get("fps") or 24)
        val = sec * fps if d.get("unit") == "frames" else sec
        if d.get("int"):
            val = int(round(val))
        g[d["node"]]["inputs"][d.get("input") or "value"] = val

    s = mapping.get("seed")
    if s and s.get("node") in g:
        g[s["node"]]["inputs"][s.get("input") or "seed"] = int(shot.get("seed") or 0)

    for i, ent in enumerate(mapping.get("images") or [], start=1):
        nid = (ent or {}).get("node")
        fn = pic_names.get(i)
        if nid and fn and nid in g:
            g[nid]["inputs"]["image"] = fn

    # 只保留这一段真正用到的参考图，其余参考槽从连线上摘掉（省显存、也避免塞错图）
    keep = set()
    for i, ent in enumerate(mapping.get("images") or [], start=1):
        if pic_names.get(i):
            n = str((ent or {}).get("node") or "")
            if n:
                keep.add(n)
    # 注意：这里必须无条件执行。keep 为空代表「这一段完全不用参考图」，
    # 也要把模板里默认连着的那些图摘掉，否则会拿着模板自带的示例图去生成。
    _prune_refs(g, keep)
    _drop_orphan_images(g)

    for i, ent in enumerate(mapping.get("videos") or [], start=1):
        nid = (ent or {}).get("node")
        fn = vid_names.get(i)
        if nid and fn and nid in g:
            key = (ent or {}).get("input") or _video_key(g, nid)
            g[nid]["inputs"][key] = fn

    proj = shot.get("_project") or "director"
    sid = shot.get("id", "shot")
    for i, ent in enumerate(mapping.get("outputs") or [], start=1):
        nid = (ent or {}).get("node")
        if nid and nid in g:
            # 多个输出节点时加序号，避免一采/二采写同名文件
            suffix = "" if i == 1 else f"_{i}"
            # 重复生成同一段时带上版本号：G01 → G01-1 → G01-2
            ver = int(shot.get("_ver") or 0)
            name = f"{sid}-{ver}" if ver > 0 else sid
            g[nid]["inputs"]["filename_prefix"] = f"{proj}/{name}{suffix}"

    for ent in mapping.get("extra") or []:
        nid = (ent or {}).get("node")
        key = (ent or {}).get("input")
        if not nid or not key or nid not in g:
            continue
        v = (ent or {}).get("value")
        if isinstance(v, str):
            s2 = v.replace("{project}", proj).replace("{shot}", sid)
            t = s2.strip()
            if re.fullmatch(r"-?\d+", t):
                v = int(t)
            elif re.fullmatch(r"-?\d*\.\d+", t):
                v = float(t)
            else:
                v = s2
        g[nid]["inputs"][key] = v
    return g


def summarize(mapping, g=None):
    """给人看的一句话摘要。"""
    if not mapping:
        return "未配置"
    parts = []
    if mapping.get("prompt"):
        parts.append("正面提示词→" + str(mapping["prompt"].get("node")))
    if mapping.get("duration"):
        d = mapping["duration"]
        parts.append(f"时长→{d.get('node')}({d.get('unit','seconds')})")
    if mapping.get("seed"):
        parts.append("种子→" + str(mapping["seed"].get("node")))
    imgs = [e.get("node") for e in (mapping.get("images") or []) if e.get("node")]
    if imgs:
        parts.append("图槽→" + "/".join(map(str, imgs)))
    vids = [e.get("node") for e in (mapping.get("videos") or []) if e.get("node")]
    if vids:
        parts.append("视频槽→" + "/".join(map(str, vids)))
    outs = [e.get("node") for e in (mapping.get("outputs") or []) if e.get("node")]
    if outs:
        parts.append("输出→" + "/".join(map(str, outs)))
    ex = [e for e in (mapping.get("extra") or []) if (e or {}).get("node") and (e or {}).get("input")]
    if ex:
        parts.append("参数→" + " · ".join(f"{e['node']}.{e['input']}={str(e.get('value'))[:12]}" for e in ex))
    et = [e for e in (mapping.get("extra_texts") or []) if (e or {}).get("node")]
    if et:
        parts.append("附加文本→" + "/".join(str(e.get("node")) for e in et))
    return " · ".join(parts) if parts else "未配置"
