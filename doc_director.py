# -*- coding: utf-8 -*-
"""
文档导演台 · 方块纪元
--------------------------------------------------
作用：丢一份「分段生成提示词」Markdown + 一个素材图文件夹，自动
  1. 解析文档 → 每段的段号/时长/英文提示词/图1图2图3 该用哪张图
  2. 到素材库按名字自动匹配图片（含模糊匹配）
  3. 把图复制进 ComfyUI input 目录（转 ASCII 安全名）
  4. 逐段提交 ComfyUI 生成，最后 ffmpeg 合成整片

依赖：Python 标准库 + 系统 ffmpeg / ffprobe
启动：python doc_director.py      浏览器 http://127.0.0.1:8771
"""
import os, re, sys, json, time, copy, uuid, glob, shutil, hashlib, subprocess, threading, webbrowser, socket, random
import urllib.request, urllib.error
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

import wfmap
import comfy_ws
import engines

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
SHOTS_PATH = os.path.join(HERE, "shots.json")
MAP_PATH = os.path.join(HERE, "workflow_map.json")
TITLES_PATH = os.path.join(HERE, "node_titles.json")
LOCKS_PATH = os.path.join(HERE, "title_locks.json")
def _bin(name):
    """优先用本工具目录下的 ffmpeg.exe / ffprobe.exe；没有就走系统 PATH。"""
    p = os.path.join(HERE, name + ".exe")
    return p if os.path.isfile(p) else name


LOG_PATH = os.path.join(HERE, "doc_director.log")


HIDDEN_PATH = os.path.join(HERE, "hidden.json")


def load_hidden():
    d = load_json(HIDDEN_PATH) or {}
    d.setdefault("assets", {})
    d.setdefault("clips", {})
    # 老版本回收站留下的标记键，保留兼容（新版已没有回收站，不会再写入）
    d.setdefault("trash_assets", {})
    d.setdefault("trash_videos", {})
    return d


def save_hidden(d):
    save_json(HIDDEN_PATH, d)


def hidden_set(kind):
    return set((load_hidden().get(kind) or {}).keys())


DEFAULT_CONFIG = {
    "doc": "",
    "assets": "",
    "comfy": "http://127.0.0.1:8188",
    "comfy_dir": "",
    "comfy_input_subdir": "doc_director",
    "comfy_output_dir": r"C:\comfyui\ComfyUI\ComfyUI\output",
    "project_name": "doc_director",
    "template": "workflow_api_template.json",
    "fps": 24,
    "onepass": True,
    "port": 8771,
    "auto_retry": 0,        # 生成失败自动重试次数（0-5）
    "auto_hide_days": 0,    # 超过多少天没动过的素材/成片自动隐藏（0=关，最长30）
    # ---- 生成引擎：只走本地 ComfyUI（云端引擎已移除）----
    "engine": "comfy",
}

TWOPASS_NODES = ["5380", "721", "722", "667", "668"]
# 各加载器节点：哪个字段是模型名 + 该去哪些目录下找
MODEL_SLOTS = {
    "UNETLoader": ("unet_name", ("diffusion_models", "unet")),
    "CLIPLoader": ("clip_name", ("text_encoders", "clip")),
    "VAELoader": ("vae_name", ("vae",)),
    "LoraLoaderModelOnly": ("lora_name", ("loras",)),
    "LoraLoader": ("lora_name", ("loras",)),
    "CheckpointLoaderSimple": ("ckpt_name", ("checkpoints",)),
}
IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
VID_EXT = (".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v")
STATE = {"running": False, "assembling": False, "current": None, "log": [], "last_error": "",
         "progress": {"node": "", "value": 0, "max": 0, "t0": 0},
         # 合成进度：done/total 段数，cur 当前段号，phase 当前阶段（归一化 / 拼接 / 完成）
         "asm_prog": {"done": 0, "total": 0, "cur": "", "phase": "", "t0": 0},
         "export": {"running": False, "total": 0, "done": 0, "current": "",
                    "copied": 0, "missing": 0, "failed": 0, "error": "", "dest": ""}}
WS = None
LOCK = threading.Lock()      # 只管内存里的 STATE（快操作）
WRLOCK = threading.RLock()   # 管 shots.json / config.json 的「读-改-写」全过程
STOP = threading.Event()
CID = "docdirector-" + uuid.uuid4().hex[:8]

CFG = {}
MAP = {}
TITLES = {}
LOCKS = set()
BAD_FILES = {}        # 读坏过的关键文件：路径 → 备份文件
CONFIG_BROKEN = False  # config.json 读坏时为 True：不允许自动回写默认值

# 访问 127.0.0.1 一律不走系统代理。
# 公司电脑 / 各种加速器经常把 http_proxy 设上，一设代理，
# 连 ComfyUI 和「探测自己端口」都会绕到代理上去，表现出来就是「ComfyUI 明明开着却显示离线」。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def load_json(p, default=None, critical=False):
    """
    读 JSON。critical=True 的文件（shots.json / config.json）读坏了会：
      1. 先复制一份到 _bak/*.bad-时间戳（原件不丢）
      2. 记一条日志
    返回 default。
    """
    try:
        with open(p, "r", encoding="utf-8") as f:
            txt = f.read()
        if not txt.strip():
            return default
        return json.loads(txt)
    except FileNotFoundError:
        return default
    except Exception as e:
        if critical:
            bak = _backup_bad(p)
            BAD_FILES[p] = bak
            log("!! %s 读不出来（%s）%s"
                % (os.path.basename(p), str(e)[:60],
                   ("，原件已复制到 _bak/" + os.path.basename(bak)) if bak else "，备份也失败了"))
        return default


def _backup_bad(p):
    """坏文件另存一份，绝不原地覆盖。"""
    try:
        d = os.path.join(HERE, "_bak")
        os.makedirs(d, exist_ok=True)
        dst = os.path.join(d, os.path.basename(p) + ".bad-" + time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(p, dst)
        return dst
    except Exception:
        return ""


def save_json(p, obj):
    """
    原子写：先写 .tmp，再 os.replace 顶替。
    这样断电/被杀进程时最多丢掉「这一次」的改动，不会留下半截 JSON。
    并把「读-改-写」用 WRLOCK 串起来，避免两个请求互相覆盖。
    """
    with WRLOCK:
        d = os.path.dirname(p) or "."
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
        tmp = p + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)
        except Exception:
            # 极少数情况（杀软锁文件等）：退回直接写，至少别把功能卡死
            try:
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(obj, f, ensure_ascii=False, indent=1)
            finally:
                try:
                    os.remove(tmp)
                except Exception:
                    pass


def _latest_bak():
    """_bak 里最新的 shots 快照。"""
    d = os.path.join(HERE, "_bak")
    if not os.path.isdir(d):
        return ""
    best, best_t = "", -1
    for fn in os.listdir(d):
        if not fn.lower().endswith(".json"):
            continue
        if not (fn.startswith("shots") or fn.startswith("shots.")):
            continue
        if ".bad-" in fn:
            continue
        p = os.path.join(d, fn)
        try:
            t = os.path.getmtime(p)
        except Exception:
            continue
        if t > best_t:
            best, best_t = p, t
    return best


def load_shots():
    """
    读 shots.json。文件存在但坏了 → 自动从 _bak 最新快照恢复，
    实在没有快照才返回空结构（并且原文件已经备份，不会二次覆盖）。
    """
    if not os.path.exists(SHOTS_PATH):
        return {"shots": []}
    d = load_json(SHOTS_PATH, None, critical=True)
    if isinstance(d, dict):
        return d
    bak = _latest_bak()
    if bak:
        bd = load_json(bak, None)
        if isinstance(bd, dict) and isinstance(bd.get("shots"), list):
            save_json(SHOTS_PATH, bd)
            log("!! shots.json 已损坏，自动从备份恢复：%s（%d 段）"
                % (os.path.basename(bak), len(bd.get("shots") or [])))
            return bd
    log("!! shots.json 已损坏且没有可用备份，本次按空数据处理（文件未删除，见 _bak/*.bad-*）")
    return {"shots": []}



def _idkey_str(nid):
    try:
        return (0, int(nid))
    except Exception:
        return (1, str(nid))


def log(msg):
    line = time.strftime("%H:%M:%S ") + str(msg)
    with LOCK:
        STATE["log"].append(line)
        if len(STATE["log"]) > 500:
            STATE["log"] = STATE["log"][-500:]
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
    # 往控制台打印是「副作用」，绝对不能让它影响业务。
    # 之前这里没有保护：bat 里 chcp 936 的控制台遇到打不出来的字符会抛异常，
    # 而这一步正好卡在「文件已经改完、HTTP 响应还没发」之间 —— 结果就是
    # 界面弹「素材不存在」，可实际东西已经处理好了。
    try:
        print(line, flush=True)
    except Exception:
        pass


# ---------------------------------------------------------------- 配置
def load_config():
    global CFG, CONFIG_BROKEN
    existed = os.path.isfile(CONFIG_PATH)
    cfg = load_json(CONFIG_PATH, None, critical=existed)
    if cfg is None and existed:
        # 文件在但读不出来：先用默认值跑起来，但绝不回写（否则你的路径全没了）
        CONFIG_BROKEN = True
        CFG = dict(DEFAULT_CONFIG)
        log("!! config.json 读不出来，本次用默认设置运行；原文件没有被覆盖，"
            "修好（或从 _bak/config.json.bad-* 找回）再重启即可")
        return CFG
    CFG = {**DEFAULT_CONFIG, **(cfg or {})}
    save_json(CONFIG_PATH, dict(CFG))
    CONFIG_BROKEN = False
    return CFG


# ---------------------------------------------------------------- 工作流映射
def load_titles():
    global TITLES
    TITLES = load_json(TITLES_PATH) or {}
    return TITLES


def save_titles(t):
    global TITLES
    TITLES = {str(k): v for k, v in (t or {}).items() if str(v).strip()}
    save_json(TITLES_PATH, TITLES)
    return TITLES


def load_locks():
    global LOCKS
    LOCKS = set(load_json(LOCKS_PATH) or [])
    return LOCKS


def save_locks():
    save_json(LOCKS_PATH, sorted(LOCKS))


def node_title(nid, node=None):
    """显示名：导入的标题 > _meta.title > 空"""
    t = TITLES.get(str(nid))
    if not t and node:
        t = (node.get("_meta") or {}).get("title") or ""
    return (t or "").strip()


def seed_titles_from_template(tpl):
    """首次用模板时，把节点名整理出来：先用 _meta.title，默认名换成「图1/种子」这类看得懂的。
    注意：只补「还没有名字」的节点 —— 以前这里是整体覆盖 node_titles.json，
    于是每次启动都会把你在 ComfyUI 里同步过来（或被锁定的）节点名冲掉。"""
    auto = {}
    for nid, n in (tpl or {}).items():
        t = ((n.get("_meta") or {}).get("title") or "").strip()
        if t and t != (n.get("class_type") or ""):
            auto[str(nid)] = t
    det = wfmap.detect(tpl, auto)
    for i, e in enumerate(det.get("images") or [], 1):
        nid = str(e.get("node"))
        cur = auto.get(nid, "")
        if not cur or cur in ("加载图像", "LoadImage", "Load Image"):
            auto[nid] = f"图{i}"
    s = det.get("seed")
    if s:
        nid = str(s.get("node"))
        if not auto.get(nid) or "Seed" in auto.get(nid, ""):
            auto[nid] = "种子"
    d = det.get("duration")
    if d:
        nid = str(d.get("node"))
        if not auto.get(nid):
            auto[nid] = "视频时长"
    # 已有的名字优先（尤其是手动改过、加了锁的），只填空缺
    add = {k: v for k, v in auto.items() if not TITLES.get(k) and k not in LOCKS}
    if add:
        save_titles({**TITLES, **add})
        log(f"节点名：识别到 {len(add)} 个（想换成你界面里的名字，用「导入节点名」）")
    return auto


_TITLE_SCAN = {"dir_mtime": 0, "file": "", "titles": {}}


def comfy_workflow_dir():
    root = os.path.dirname(os.path.normpath(CFG["comfy_output_dir"]))
    return os.path.join(root, "user", "default", "workflows")


def scan_comfy_titles(force=False):
    """
    扫 ComfyUI 的 user/default/workflows，挑和当前模板节点 ID 重合度最高的那份工作流，
    把 nodes[].title 拿过来。这样你在 ComfyUI 里改了节点名、Ctrl+S 一存，这里就能同步。
    返回 (titles, 文件路径, 错误说明)
    """
    d = comfy_workflow_dir()
    if not os.path.isdir(d):
        return {}, "", "找不到 ComfyUI 工作流目录：" + d

    files, newest = [], 0
    for dirpath, _, fs in os.walk(d):
        for f in fs:
            if not f.lower().endswith(".json"):
                continue
            p = os.path.join(dirpath, f)
            try:
                mt = os.path.getmtime(p)
            except Exception:
                continue
            files.append((mt, p))
            newest = max(newest, mt)
    if not files:
        return {}, "", "ComfyUI 的 workflows 目录里还没有工作流（先在 ComfyUI 里 Ctrl+S 保存一次）"

    if not force and newest and newest == _TITLE_SCAN.get("dir_mtime"):
        return _TITLE_SCAN.get("titles", {}), _TITLE_SCAN.get("file", ""), ""

    ids = set((load_template() or {}).keys())
    best = None      # (重合数, mtime, path, obj)
    for mt, p in files:
        obj = load_json(p)
        if not isinstance(obj, dict) or not isinstance(obj.get("nodes"), list):
            continue
        nids = {str(n.get("id")) for n in obj["nodes"] if n.get("id") is not None}
        ov = len(nids & ids)
        if best is None or ov > best[0] or (ov == best[0] and mt > best[1]):
            best = (ov, mt, p, obj)
    if not best:
        return {}, "", "workflows 目录里的文件都不是 ComfyUI 界面格式，读不出节点名"
    ov, mt, p, obj = best
    if ov < 3:
        return {}, p, f"最像的工作流只对得上 {ov} 个节点，可能不是同一套，没敢用"

    titles = {}
    for n in obj["nodes"]:
        nm = (n.get("title") or "").strip()
        nid = n.get("id")
        if nm and nid is not None and str(nid) not in LOCKS:
            titles[str(nid)] = nm
    _TITLE_SCAN.update({"dir_mtime": newest, "file": p, "titles": titles})
    return titles, p, ""


_TITLE_COMFY = {"t": 0.0}


def _is_ours(extra):
    """判断这条任务是不是本系列工具提交的（新旧导演台都算）。"""
    try:
        cid = str((extra or {}).get("client_id") or "")
        return cid.startswith("docdirector-") or cid.startswith("director-")
    except Exception:
        return False


def refresh_titles_from_comfy(force=False):
    """
    从 ComfyUI 队列/历史里的 prompt 读 _meta.title。
    比扫磁盘更实时：你在 ComfyUI 里改了节点名，只要点过一次「运行」，这里就能拿到。
    会跳过本工具自己提交的任务，免得把旧名字反灌回来。
    """
    now = time.time()
    if not force and now - _TITLE_COMFY["t"] < 8:
        return 0
    _TITLE_COMFY["t"] = now

    cands = []
    try:
        q = comfy_get("/queue", timeout=8)
        for it in (q.get("queue_running") or []) + (q.get("queue_pending") or []):
            if len(it) >= 3 and isinstance(it[2], dict):
                if len(it) > 3 and _is_ours(it[3]):
                    continue
                cands.append(it[2])
    except Exception:
        pass
    try:
        h = comfy_get("/history", timeout=20)
        for pid, rec in h.items():
            pr = rec.get("prompt") or []
            if len(pr) >= 3 and isinstance(pr[2], dict):
                if len(pr) > 3 and _is_ours(pr[3]):
                    continue
                cands.append(pr[2])
    except Exception:
        pass
    if not cands:
        return 0

    cur_n = len(load_template() or {})
    big = [c for c in cands if len(c) >= max(cur_n - 10, 20)]
    if not big:
        return 0
    g = max(big, key=lambda c: (
        len(set(c.keys()) & set((load_template() or {}).keys())),
        sum(1 for v in c.values() if (v.get("class_type") or "") in wfmap.IMG_CLASSES),
        len(c)))
    got, changed = {}, 0
    for nid, n in g.items():
        t = ((n.get("_meta") or {}).get("title") or "").strip()
        ct = (n.get("class_type") or "")
        # 默认名（等于类型名 / 「加载图像」这种）不要，免得覆盖掉「图1」这类规范名
        if not t or t == ct or t in ("加载图像", "LoadImage", "Load Image"):
            continue
        if str(nid) in LOCKS:
            continue          # 手动命名过的，不让自动同步覆盖
        got[str(nid)] = t
        if TITLES.get(str(nid)) != t:
            changed += 1
    if changed:
        save_titles({**TITLES, **got})
        log(f"从 ComfyUI 同步了 {changed} 个节点名")
    return changed


def title_watcher():
    """
    每 5 秒看一眼 ComfyUI 的 workflows 目录有没有新保存。
    你在 ComfyUI 里 Ctrl+S 之后，这边几秒内自动把节点名同步过来，不用点按钮。
    """
    last = 0.0
    while True:
        time.sleep(5)
        try:
            d = comfy_workflow_dir()
            if not os.path.isdir(d):
                continue
            newest = 0.0
            for dirpath, _, fs in os.walk(d):
                for f in fs:
                    if f.lower().endswith(".json"):
                        try:
                            newest = max(newest, os.path.getmtime(os.path.join(dirpath, f)))
                        except Exception:
                            pass
            if not newest or newest == last:
                continue
            last = newest
            titles, path, err = scan_comfy_titles(force=True)
            if not titles:
                continue
            changed = {k: v for k, v in titles.items() if TITLES.get(k) != v}
            if changed:
                save_titles({**TITLES, **titles})
                log(f"检测到 ComfyUI 保存了新工作流，同步 {len(changed)} 个节点名"
                    f"（{'、'.join(list(changed.values())[:4])}…）")
        except Exception:
            pass


def start_title_watcher():
    t = threading.Thread(target=title_watcher, daemon=True)
    t.start()
    return t


def import_from_comfy():
    """
    从 ComfyUI 的队列/历史里抓最近一次「运行」提交的完整 API prompt。
    用户只要在 ComfyUI 里点过运行，这里就能拿到他那份完完整整的工作流（含所有图槽、视频槽、节点名）。
    """
    cands = []
    try:
        q = comfy_get("/queue", timeout=10)
        for item in (q.get("queue_running") or []) + (q.get("queue_pending") or []):
            if len(item) >= 3 and isinstance(item[2], dict):
                if len(item) > 3 and _is_ours(item[3]):
                    continue          # 跳过本工具自己提交的任务
                cands.append(("队列", item[2]))
    except Exception:
        pass
    try:
        h = comfy_get("/history", timeout=25)
        for pid, rec in h.items():
            pr = rec.get("prompt") or []
            if len(pr) >= 3 and isinstance(pr[2], dict):
                if len(pr) > 3 and _is_ours(pr[3]):
                    continue          # 跳过本工具自己提交的任务
                cands.append(("历史", pr[2]))
    except Exception:
        pass
    if not cands:
        return None, ("ComfyUI 队列里只有本工具自己提交的任务，抓不到你在界面上的那份工作流。\n\n"
                      "请在 ComfyUI 里手动点一次「运行」（跑起来就行），再回来点这里。")

    # 选哪一份：先看和当前模板的节点重合度（确保是同一套工作流），再看图/视频节点数量
    tpl_ids = set((load_template() or {}).keys())

    def _rank(c):
        g = c[1]
        ids = set(g.keys())
        ov = len(ids & tpl_ids)
        ni = sum(1 for v in g.values() if (v.get("class_type") or "") in wfmap.IMG_CLASSES)
        nv = sum(1 for v in g.values() if (v.get("class_type") or "") in wfmap.VIDEO_CLASSES)
        # 先确认是同一套工作流（重合节点够多），再优先图/视频节点多的那份
        return (1 if ov >= 20 else 0, ni + nv, ov, len(g))

    src, g = max(cands, key=_rank)
    if len(g) < 10:
        return None, "抓到的那份工作流太小（%d 个节点），不像完整流程，没导入。" % len(g)

    name = "comfyui_latest.json"
    save_json(os.path.join(HERE, name), g)
    CFG["template"] = name
    save_json(CONFIG_PATH, CFG)

    titles = {}
    for nid, n in g.items():
        t = ((n.get("_meta") or {}).get("title") or "").strip()
        ct = (n.get("class_type") or "")
        if t and t != ct and t not in ("加载图像", "LoadImage", "Load Image"):
            titles[str(nid)] = t
    if titles:
        save_titles({**TITLES, **titles})
    return g, ""


def load_template():
    p = os.path.join(HERE, CFG["template"])
    return load_json(p)


def ensure_mapping(force=False):
    """模板没变就复用 workflow_map.json；变了/强制就重新探测。"""
    global MAP
    tpl = load_template()
    if not tpl:
        raise RuntimeError("找不到工作流模板：" + str(CFG.get("template")))
    if not TITLES:
        seed_titles_from_template(tpl)
    h = wfmap.template_hash(tpl)
    saved = load_json(MAP_PATH) or {}
    if not force and saved.get("template_hash") == h and saved.get("prompt"):
        saved.setdefault("images", [])
        saved.setdefault("videos", [])
        saved.setdefault("outputs", [])
        MAP = saved
        log("工作流映射：沿用已保存配置（" + wfmap.summarize(MAP) + "）")
        return MAP
    m = wfmap.detect(tpl, TITLES)
    m["template_hash"] = h
    m["fps"] = CFG.get("fps") or 24
    # 换了模板后，指向不存在节点的旧参数直接丢弃（节点 ID 变了就留着也没用）
    old_extra = [e for e in (saved.get("extra") or [])
                 if isinstance(e, dict) and str(e.get("node") or "") in tpl]
    m["extra"] = old_extra
    # 附加文本槽（每段可勾选是否注入的文本节点）也要跨模板变更保留下来
    old_texts = [e for e in (saved.get("extra_texts") or [])
                 if isinstance(e, dict) and str(e.get("node") or "") in tpl]
    m["extra_texts"] = old_texts
    MAP = wfmap.clean(m)
    save_json(MAP_PATH, MAP)
    log(("重新识别" if force else "自动识别") + "工作流：" + wfmap.summarize(MAP))
    return MAP


def get_mapping():
    global MAP
    if not MAP:
        try:
            ensure_mapping()
        except Exception as e:
            log("工作流映射失败：" + str(e))
            MAP = {}
    return MAP


# ---------------------------------------------------------------- 素材库
def list_assets(include_hidden=False):
    """include_hidden=True 用于「解析/重新匹配」：被自动隐藏的素材也要能匹配上，
    否则隐藏过的图一重新解析就变成未匹配。"""
    d = CFG["assets"]
    if not os.path.isdir(d):
        return []
    hid = set() if include_hidden else hidden_set("assets")
    out = [f for f in os.listdir(d)
           if f.lower().endswith(IMG_EXT) and f not in hid]
    # 最新上传的排最前（左上角）：按文件修改时间倒序，同时间的按名字兜底，保证顺序稳定
    def _key(f):
        try:
            return (-os.path.getmtime(os.path.join(d, f)), f.lower())
        except Exception:
            return (0.0, f.lower())
    out.sort(key=_key)
    return out


def list_videos(include_hidden=False):
    d = CFG["assets"]
    if not os.path.isdir(d):
        return []
    hid = set() if include_hidden else hidden_set("videos")
    return [f for f in sorted(os.listdir(d)) if f.lower().endswith(VID_EXT) and f not in hid]


def _clean_name(s):
    s = (s or "").strip()
    s = s.replace("✅", "").replace("☑", "")
    s = re.sub(r"[（(]\s*可选\s*[)）]", "", s)
    s = s.replace("**", "").strip()
    return s.strip()


def _bigrams(s):
    s = re.sub(r"[（）()\s\-_·・,，。.、]", "", s)
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s}


DOC_EXT = (".md", ".markdown")
ASSET_DIR_HINTS = ("资产", "素材", "参考", "图片", "图", "asset", "assets",
                   "image", "images", "img", "ref", "refs", "pic", "pics")


def _count_images(d):
    try:
        return sum(1 for f in os.listdir(d) if f.lower().endswith(IMG_EXT))
    except Exception:
        return 0


def detect_paths(p, _deep=True):
    """
    给一个路径（可以是「装着 .md 和 资产图 的文件夹」，也可以是 .md 本身，或素材文件夹），
    自己认出「文档」和「素材文件夹」。本层找不到会再往下钻一层。
    返回 dict：doc / assets / n_images / n_shots / docs（候选）/ asset_dirs / error
    """
    p = (p or "").strip().strip('"').strip("'")
    res = {"input": p, "base": "", "doc": "", "assets": "", "docs": [],
           "asset_dirs": [], "n_images": 0, "error": ""}
    if not p:
        res["error"] = "路径是空的"
        return res
    if os.path.isfile(p):
        base = os.path.dirname(p)
        if p.lower().endswith(DOC_EXT):
            res["doc"] = p
    elif os.path.isdir(p):
        base = p
    else:
        res["error"] = "这个路径不存在：" + p
        return res
    res["base"] = base

    # --- 文档：同层找 .md；有多个就挑名字最像「分段提示词」的，再不行取最新的 ---
    if not res["doc"]:
        try:
            files = [f for f in os.listdir(base) if f.lower().endswith(DOC_EXT)]
        except Exception:
            files = []
        res["docs"] = [os.path.join(base, f) for f in sorted(files)]
        if len(files) == 1:
            res["doc"] = res["docs"][0]
        elif files:
            def _score(f):
                t = f.lower()
                s = sum(1 for kw in ("分段", "提示词", "分镜", "shot", "prompt") if kw in t)
                try:
                    mt = os.path.getmtime(os.path.join(base, f))
                except Exception:
                    mt = 0
                return (s, mt)
            res["doc"] = os.path.join(base, sorted(files, key=_score)[-1])

    # --- 素材文件夹：同层的子目录里，哪张图多 / 名字像「资产图」就用哪个 ---
    subs = []
    try:
        subs = [d for d in sorted(os.listdir(base))
                if os.path.isdir(os.path.join(base, d))]
    except Exception:
        pass
    cands = []
    for d in subs:
        full = os.path.join(base, d)
        n = _count_images(full)
        if not n:
            continue
        low = d.lower()
        cands.append({"dir": full, "n": n,
                      "hint": any(h in low for h in ASSET_DIR_HINTS)})
    res["asset_dirs"] = cands
    if cands:
        cands.sort(key=lambda x: (x["hint"], x["n"]), reverse=True)
        res["assets"] = cands[0]["dir"]
    elif _count_images(base):
        res["assets"] = base            # 图片和文档就在同一层
    res["n_images"] = _count_images(res["assets"]) if res["assets"] else 0
    res["n_shots"] = 0
    if res["doc"]:
        try:
            with open(res["doc"], encoding="utf-8-sig", errors="replace") as f:
                res["n_shots"] = len(re.findall(r"^###\s+G\d+", f.read(), re.M))
        except Exception:
            pass

    # --- 这一层啥都没找到？再往下钻一层 ---
    # 常见结构是「<项目>/<第一集>/xxx.md + 资产图/」，给上层目录也能认出来
    if _deep and not res["doc"] and not res["assets"] and subs:
        scored = []
        for d in subs:
            full = os.path.join(base, d)
            n_doc = len([f for f in (os.listdir(full) if os.path.isdir(full) else [])
                         if f.lower().endswith(DOC_EXT)])
            score = n_doc * 10 + _count_images(full) * 2
            if score:
                scored.append((score, full))
        scored.sort(key=lambda x: x[0], reverse=True)
        best = None
        for _sc, full in scored[:6]:
            inner = detect_paths(full, _deep=False)
            # 优先「真的有分段标题」的目录，其次图多、段多
            rank = (1 if inner["n_shots"] else 0, inner["n_images"], inner["n_shots"])
            if best is None or rank > best[0]:
                best = (rank, inner, full)
        if best:
            _rank, inner, full = best
            for k in ("doc", "docs", "assets", "asset_dirs", "n_images", "n_shots"):
                res[k] = inner[k]
            res["picked"] = full              # 实际用的是哪个子目录
    if res["doc"] and not res["n_shots"]:
        res["warn"] = "这份 .md 里没找到「### G01」这样的分段标题，可能不是分段提示词文档"
    return res


def match_asset(raw, files):
    """
    把一个文档里的图槽写法匹配到素材库里的真实文件名。
    只认「名字一致」——不做模糊匹配：文档写什么就必须有什么，否则留空让你手动选。
    """
    raw = _clean_name(raw)
    if not raw:
        return None, 0.0
    if raw in ("无", "—", "-", "无图", "none", "N/A"):
        return None, 0.0
    # 1) 完全一致
    if raw in files:
        return raw, 1.0
    # 2) 文档里没写扩展名，补 .png 后一致
    if raw + ".png" in files:
        return raw + ".png", 0.99
    # 3) 扩展名不同但主名一致（草原.jpg ↔ 草原.png）
    base = os.path.splitext(raw)[0]
    for f in files:
        if os.path.splitext(f)[0] == base:
            return f, 0.98
    # 4) 去掉括号补充说明后主名一致
    #    （周桂兰人物设定图 ↔ 周桂兰人物设定图（正面全身+面部特写.png）
    def _stem(s):
        return re.split(r"[（(]", os.path.splitext(s)[0])[0].strip()
    rstem = _stem(raw)
    # 中文角色名多为 2~3 字（林野/秦霜/赵天磊），原阈值 4 会让它们全部匹配不上；
    # 这里比对的是「去括号后的完整主名是否相等」，不是模糊匹配，放低到 2 不会误伤。
    if len(rstem) >= 2:
        for f in files:
            if _stem(f) == rstem:
                return f, 0.97
    return None, 0.0


def safe_copy(src_path):
    """把素材图复制进 ComfyUI input 子目录，返回可给 LoadImage 用的相对路径。"""
    sub = CFG["comfy_input_subdir"]
    root = os.path.dirname(os.path.normpath(CFG["comfy_output_dir"]))
    dst_dir = os.path.join(root, "input", sub)
    if not os.path.isdir(root):
        dst_dir = os.path.join(r"C:\comfyui\ComfyUI\ComfyUI\input", sub)
    os.makedirs(dst_dir, exist_ok=True)
    h = hashlib.md5()
    with open(src_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    ext = os.path.splitext(src_path)[1].lower() or ".png"
    name = "asset_" + h.hexdigest()[:10] + ext
    dst = os.path.join(dst_dir, name)
    if not os.path.exists(dst) or os.path.getsize(dst) != os.path.getsize(src_path):
        shutil.copy2(src_path, dst)
    return sub + "/" + name


# ---------------------------------------------------------------- 文档解析
# 段标题识别（宽松版）：段号、时间码、时长的顺序和写法随意，能认出来就行
#   认得：### G01 · 00:00-00:12（12 秒）｜xxx      （原格式，不变）
#         ### G01 00:00-00:12 (12s) xxx
#         ### G1 · 12秒 · xxx                     （没时间码，按前面段落累出来）
#         #### 00:00-00:12 · G01 · xxx            （乱序、换标题级别都行）
#         G04 00:30-00:38（8秒）开场              （甚至不写 # 也能认）
#         ### 第1段｜穿越醒来｜16秒｜H3 Ref2VA    （「第N段」板式也认）
SEC_ID_RE = re.compile(r"(?i)(?<![A-Za-z0-9])G(\d{1,3})(?![A-Za-z0-9])")
CN_SEG_RE = re.compile(r"第\s*(\d{1,3})\s*段")          # 「第1段」「第 01 段」
SEC_TIME_RE = re.compile(r"(\d{1,2}):([0-5]\d)\s*(?:-|–|—|~|～|到|至)\s*(\d{1,2}):([0-5]\d)")
SEC_DUR_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:秒|s\b|S\b)")
SEC_DUR2_RE = re.compile(r"时长\s*[:：]?\s*(\d+(?:\.\d+)?)")


def _match_section(ln):
    """尝试把一行认成段标题。认不出返回 None。

    条件：有段号（G01 或 第1段），并且（有时间码 或 有时长秒数）。
    段号/时间/时长顺序随意。表格行一律不当标题。
    """
    s = ln.strip()
    if s.startswith("|"):
        return None                  # 表格行（段总表/上传顺序表）不当标题
    core = s.lstrip("#").strip()
    if core.startswith("**") and core.endswith("**"):
        core = core.strip("*").strip()
    if not core:
        return None
    # 必须像标题：# 开头、加粗、或者本来就很短的一行
    heading_like = ln.lstrip().startswith("#") or s.startswith("**") or len(core) <= 40
    if not heading_like:
        return None
    mid = SEC_ID_RE.search(core)
    seg = None if mid else CN_SEG_RE.search(core)
    if not mid and not seg:
        return None
    tm = SEC_TIME_RE.search(core)
    dur = None
    m2 = SEC_DUR_RE.search(core) or SEC_DUR2_RE.search(core)
    if m2:
        dur = float(m2.group(1))
    if tm:
        t_sec = (int(tm.group(3)) * 60 + int(tm.group(4))) - (int(tm.group(1)) * 60 + int(tm.group(2)))
        if dur is None:
            dur = float(t_sec)
    if dur is None:
        return None
    dur = int(dur) if float(dur).is_integer() else float(dur)
    if dur <= 0:
        return None
    sid = "G%02d" % int((mid or seg).group(1))
    # 标题 = 整行去掉已识别的片段后剩下的
    title = SEC_TIME_RE.sub("", core)
    title = SEC_DUR_RE.sub("", title, count=1)
    title = SEC_DUR2_RE.sub("", title, count=1)
    title = (SEC_ID_RE if mid else CN_SEG_RE).sub("", title, count=1)
    title = re.sub(r"^[·｜|\-–—:：\s]+", "", title)
    title = re.sub(r"[·｜|\-–—:：\s]+$", "", title)
    title = re.sub(r"[（(]\s*[)）]", "", title)              # 残留的空括号
    title = re.sub(r"^[（）()\s｜|\-–—·:：]+", "", title)    # 开头残留的括号/分隔符
    title = title.replace("**", "").replace("`", "").strip()
    # 竖线标题掐掉中间片段后会留双 ｜，压成单重并去掉空段
    parts = [p.strip() for p in re.split(r"[｜|]", title) if p.strip()]
    title = "｜".join(parts)
    if tm:
        tc = "%d:%02d-%d:%02d" % (int(tm.group(1)), int(tm.group(2)),
                                  int(tm.group(3)), int(tm.group(4)))
    else:
        tc = None
    return {"id": sid, "tc": tc, "sec": dur, "title": title}

MAX_PIC_SLOTS = 9
MAX_VID_SLOTS = 5
_PIC_NONE = ("", "—", "-", "－", "无", "无图", "none", "n/a")


def _blank_slot(v):
    """图槽单元格是不是「没写图」。"""
    return (v or "").strip().lower() in _PIC_NONE


def _strip_md(s):
    return s.replace("**", "").replace("`", "").strip()


def _split_generic(lines):
    """通用提示词文档兜底：没有 G 段号时，按 空行 / 分隔线 / 序号 / 标题 切段。

    每块 = 一段；段号自动编 G01、G02…；时长统一 8 秒（可在界面改）。
    """
    num_re = re.compile(r"^\s*\d{1,3}\s*[.、）)]\s*")
    cn_re = re.compile(r"^\s*[一二三四五六七八九十]{1,3}\s*[、.]\s*")
    hr_re = re.compile(r"^\s*([-*_]\s*){3,}$")
    groups, cur = [], []

    def flush():
        if cur:
            groups.append(list(cur))
            cur.clear()

    for ln in lines:
        s = ln.strip()
        if hr_re.match(s) or s.startswith("#"):
            flush()
            continue
        if num_re.match(ln) or cn_re.match(ln):
            flush()
            ln = num_re.sub("", ln, count=1)
            ln = cn_re.sub("", ln, count=1)
            cur.append(ln.rstrip())
            continue
        if not s:
            flush()
            continue
        cur.append(ln.rstrip())
    flush()

    out = []
    n = 0
    for blk in groups:
        blk = [l for l in blk if l.strip()]
        text = "\n".join(blk).strip()
        if len(text) < 2:
            continue          # 太短的（比如孤零零一个词）不当成段
        n += 1
        out.append({"id": "G%02d" % n, "tc": None, "sec": 8,
                    "title": text.splitlines()[0][:16], "lines": blk, "generic": True})
    return out


def parse_doc(path):
    """解析分段提示词文档，返回 shots 列表。"""
    with open(path, "r", encoding="utf-8-sig") as f:
        lines = f.read().splitlines()

    # 先抓段总表（作为图槽兜底 & 内容说明）
    # 表头固定是「段号|时间码|时长|镜号|内容|图1|图2|…」，所以第 5 列往后就按顺序当图1..图9，
    # 不再写死只读 3 个图列（文档写了图4~图9 也能对上）。
    table = {}
    for ln in lines:
        s0 = ln.strip()
        if not s0.startswith("|"):
            continue
        cells = [c.strip() for c in s0.strip("|").split("|")]
        if len(cells) < 5 or not re.match(r"^G\d+$", cells[0]):
            continue
        rec = {"tc": cells[1], "sec": cells[2], "shot": cells[3], "title": cells[4]}
        for i, c in enumerate(cells[5:5 + MAX_PIC_SLOTS], start=1):
            rec["pic%d" % i] = c
        table[cells[0]] = rec

    # 按「宽松段标题」切块：段号/时间/时长顺序随意，写法宽容
    blocks, cur = [], None
    run_sec = 0
    for ln in lines:
        hit = _match_section(ln)
        if hit:
            if hit["tc"] is None:
                # 没写时间码：按前面段落时长累出来
                start = run_sec
                end = start + int(round(hit["sec"]))
                hit["tc"] = "%02d:%02d-%02d:%02d" % (start // 60, start % 60, end // 60, end % 60)
            else:
                hh, mm = hit["tc"].split("-")[1].split(":")
                run_sec = int(hh) * 60 + int(mm)
            cur = {"id": hit["id"], "tc": hit["tc"].replace(" ", ""), "sec": hit["sec"],
                   "title": hit["title"], "lines": []}
            blocks.append(cur)
        elif ln.startswith("## ") and cur:
            # 附录等大标题，停止收集（但它自己像段标题的话上面已经先认走了）
            cur = None
        elif cur is not None:
            cur["lines"].append(ln)

    # 通用文档兜底：一个 G 段都没认出来时，按空行/分隔线/序号自动切段
    if not blocks:
        blocks = _split_generic(lines)
        if blocks:
            log("文档里没有 G 段标题，按通用提示词文档处理：自动切成 "
                + str(len(blocks)) + " 段（每段 8 秒，图1 需在界面手选）")

    assets = list_assets(include_hidden=True)
    vidlib = list_videos(include_hidden=True)
    shots = []
    for b in blocks:
        raw_pics = {}
        raw_vids = {}
        cn_prompt = ""
        en_prompt = ""
        body = b["lines"]

        # 找「上传图」行（支持 图1..图9 和 视频1..视频5）
        for ln in body:
            if ("上传图" in ln or "上传素材" in ln) and "：" in ln:
                seg = ln.split("：", 1)[1]
                for part in re.split(r"[｜|]", seg):
                    pm = re.search(r"图\s*(\d+)", part)
                    vm = re.search(r"视频\s*(\d+)", part)
                    if pm:
                        slot, kind, anchor = int(pm.group(1)), "pic", pm.end()
                    elif vm:
                        slot, kind, anchor = int(vm.group(1)), "vid", vm.end()
                    else:
                        continue
                    q = re.search(r"`([^`]+)`", part)
                    if q:
                        val = q.group(1).strip()
                    else:
                        val = part[anchor:]
                        val = re.split(r"[（(]", val)[0]
                        val = _clean_name(val)
                    (raw_pics if kind == "pic" else raw_vids)[slot] = val
                break

        # 「上传顺序表」式表格：| Picture 1 | lin_identity.png | 职责 | ...
        for ln in body:
            t = ln.strip()
            if not t.startswith("|"):
                continue
            m = re.match(r"^\|\s*(Picture|Video|图|视频|Img|Image)\s*(\d+)\s*\|", t, re.I)
            if not m:
                continue
            cells = [c.strip().strip("`") for c in t.strip("|").split("|")]
            if len(cells) < 2 or not cells[1]:
                continue
            val = cells[1]
            # 括号里的内容是补充说明（如「周桂兰人物设定图（正面全身 + 面部特写）」），
            # 不参与匹配——去掉括号段，剩下主干名去对素材库（双向包含照样能对上）
            head = re.split(r"[（(]", val)[0].strip()
            if head:
                val = head
            if val.lower() in ("filename", "文件名", "file", "—", "-"):
                continue
            slot = int(m.group(2))
            if m.group(1).lower() in ("video",) or m.group(1) == "视频":
                if slot <= MAX_VID_SLOTS:
                    raw_vids.setdefault(slot, val)
            elif slot <= MAX_PIC_SLOTS:
                raw_pics.setdefault(slot, val)

        # 抓英文版 / 中文版代码块
        def grab(flag):
            hit = False
            for i, ln in enumerate(body):
                if flag in ln:
                    hit = True
                    continue
                if hit:
                    if ln.strip().startswith("```"):
                        buf = []
                        for ln2 in body[i + 1:]:
                            if ln2.strip().startswith("```"):
                                return "\n".join(buf).strip()
                            buf.append(ln2)
                        return "\n".join(buf).strip()
            return ""

        en_prompt = grab("【英文版】")
        cn_prompt = grab("【中文版】")
        # 可选标记：导出文档时会写，重新导入后语言选择不丢；没写就默认英文
        lang = "en"
        for ln in body:
            if "喂给模型" in ln and "：" in ln:
                if "中文" in ln.split("：", 1)[1]:
                    lang = "cn"
                break
        # 只写了【中文版】没写【英文版】：自动按中文喂，不会发出空提示词
        if not en_prompt and cn_prompt:
            lang = "cn"
            en_prompt = cn_prompt
        # 连标记都没有：正文里有代码块就取第一个代码块当提示词；没有就用整块正文
        if not en_prompt and not cn_prompt:
            buf = None
            for ln in body:
                t = ln.strip()
                if buf is None:
                    if t.startswith("```"):
                        buf = []
                    continue
                if t.startswith("```"):
                    break
                buf.append(ln)
            if buf:
                en_prompt = "\n".join(buf).strip()
            else:
                en_prompt = "\n".join(
                    l for l in body
                    if "上传图" not in l and "上传素材" not in l and "喂给模型" not in l
                ).strip().replace("**", "").replace("`", "")

        tb = table.get(b["id"], {})
        # 总表里写了的图列也算（这样文档总表写了图4/图5 也能补进来）
        tb_slots = [i for i in range(1, MAX_PIC_SLOTS + 1)
                    if not _blank_slot(tb.get("pic%d" % i))]
        resolved, vresolved = {}, {}
        for slot in sorted(set(raw_pics) | set(tb_slots) | {1, 2, 3}):
            if slot > MAX_PIC_SLOTS:
                continue
            raw = raw_pics.get(slot) or ""
            if not raw and tb:
                raw = tb.get("pic%d" % slot, "")
            raw = _clean_name(raw)
            if raw in ("—", "-", "无图", "无"):
                raw = ""
            f, score = match_asset(raw, assets) if raw else (None, 0.0)
            resolved[slot] = {"raw": raw, "file": f or "", "score": score}

        for slot in sorted(raw_vids):
            if slot > 5:
                continue
            raw = _clean_name(raw_vids.get(slot) or "")
            if raw in ("—", "-", "无", "无视频"):
                raw = ""
            f, score = match_asset(raw, vidlib) if raw else (None, 0.0)
            vresolved[slot] = {"raw": raw, "file": f or "", "score": score}

        all_empty = (not any((resolved.get(i) or {}).get("file") for i in range(1, MAX_PIC_SLOTS + 1))
                     and not any((vresolved.get(i) or {}).get("file") for i in range(1, MAX_VID_SLOTS + 1)))
        # 文档是否「真的写了图」：写了名字只是没匹配上 ≠ 不要图（别自动跳过，界面手选即可）
        wrote_any = (any((raw_pics.get(i) or "").strip() for i in range(1, MAX_PIC_SLOTS + 1))
                     or any((raw_vids.get(i) or "").strip() for i in range(1, MAX_VID_SLOTS + 1))
                     or bool(tb_slots))
        auto_skip = bool(all_empty) and not wrote_any      # 文档明确「无图」才自动跳过
        # 这一段文档里明确写了几号图槽（用于表格只显示需要的行数）
        doc_slots = [i for i, v in raw_pics.items() if (v or "").strip()] + tb_slots
        n_pic = min(MAX_PIC_SLOTS, max(doc_slots) if doc_slots else 3)
        doc_vslots = [i for i, v in raw_vids.items() if (v or "").strip()]
        n_vid = min(MAX_VID_SLOTS, max(doc_vslots) if doc_vslots else 0)
        if b.get("generic"):
            # 通用文档：没匹配到素材不代表要跳过，图1 可以在界面手选
            note = "通用文档自动分段 · 图1未匹配（界面手选或标记跳过）" if all_empty else ""
            shot = {
                "id": b["id"],
                "title": b["title"] or tb.get("title", ""),
                "tc": b["tc"] or tb.get("tc", ""),
                "sec": b["sec"] or 8,
                "prompt": en_prompt,
                "prompt_cn": cn_prompt,
                "lang": lang,
                "extra_texts": {},
                "skip": False,
                "status": "pending",
                "out": "",
                "seed": 0,
                "pic_n": min(MAX_PIC_SLOTS, max(doc_slots) if doc_slots else 1),
                "vid_n": n_vid,
                "pic_more": 0,
                "note": note
            }
        else:
            if auto_skip:
                note = "文档标注无图，默认跳过（纯黑/字幕段）"
            elif all_empty and wrote_any:
                note = "图槽写了名字但没匹配到素材，界面手选或标记跳过"
            else:
                note = ""
            shot = {
                "id": b["id"],
                "title": b["title"] or tb.get("title", ""),
                "tc": b["tc"] or tb.get("tc", ""),
                "sec": b["sec"] or 12,
                "prompt": en_prompt,
                "prompt_cn": cn_prompt,
                "lang": lang,               # 喂给模型的语言：en=【英文版】 cn=【中文版】
                "extra_texts": {},          # {节点id: {"on": bool, "text": str}} 附加文本槽
                "skip": auto_skip,
                "status": "pending",
                "out": "",
                "seed": 0,
                "pic_n": n_pic,
                "vid_n": n_vid,
                "pic_more": 0,
                "note": note
            }
        for i in range(1, MAX_PIC_SLOTS + 1):
            r = resolved.get(i) or {"raw": "", "file": "", "score": 0.0}
            shot["pic%d" % i] = r["file"]
            shot["raw%d" % i] = r["raw"]
            shot["score%d" % i] = r["score"]
        for i in range(1, MAX_VID_SLOTS + 1):
            r = vresolved.get(i) or {"raw": "", "file": "", "score": 0.0}
            shot["vid%d" % i] = r["file"]
            shot["rawvid%d" % i] = r["raw"]
        shots.append(shot)
    return shots


def build_doc_markdown(shots):
    """把当前分段（含界面里改过的提示词 / 图槽 / 语言）写成标准格式的 .md。"""
    out = []
    for s in shots:
        if s.get("deleted"):
            continue
        sid = s.get("id") or "G00"
        tc = (s.get("tc") or "").strip()
        try:
            sec = int(round(float(s.get("sec") or 0)))
        except Exception:
            sec = 0
        head = "### " + sid
        if tc:
            head += " \u00b7 " + tc
        head += "\uff08%d \u79d2\uff09" % sec
        title = (s.get("title") or "").strip()
        if title:
            head += "\uff5c" + title
        out.append(head)
        out.append("")

        pics, vids = [], []
        for k in range(1, 10):
            f = (s.get("pic%d" % k) or "").strip()
            if f:
                pics.append("\u56fe%d `%s`" % (k, f))
        for k in range(1, 6):
            f = (s.get("vid%d" % k) or "").strip()
            if f:
                vids.append("\u89c6\u9891%d `%s`" % (k, f))
        body_pics = "\uff5c".join(pics + vids) if (pics or vids) else "\u65e0\u56fe"
        out.append("**\u4e0a\u4f20\u56fe**\uff1a" + body_pics)
        if (s.get("lang") or "en") == "cn":
            out.append("")
            out.append("**\u5582\u7ed9\u6a21\u578b**\uff1a\u4e2d\u6587\u7248")
        out.append("")
        out.append("**\u3010\u82f1\u6587\u7248\u3011**")
        out.append("")
        out.append("```text")
        out.append((s.get("prompt") or "").strip())
        out.append("```")
        out.append("")
        out.append("**\u3010\u4e2d\u6587\u7248\u3011**")
        out.append("")
        out.append("```text")
        out.append((s.get("prompt_cn") or "").strip())
        out.append("```")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def list_backups():
    """_bak 里所有 shots 相关的快照，最新的排前面。"""
    d = os.path.join(HERE, "_bak")
    items = []
    if not os.path.isdir(d):
        return items
    for fn in os.listdir(d):
        if not fn.lower().endswith(".json"):
            continue
        if not (fn.startswith("shots") or fn.startswith("shots.")):
            continue
        p = os.path.join(d, fn)
        if not os.path.isfile(p):
            continue
        cnt = None
        try:
            data = load_json(p) or {}
            cnt = len(data.get("shots") or [])
        except Exception:
            cnt = None
        items.append({
            "file": fn,
            "mtime": int(os.path.getmtime(p)),
            "size": os.path.getsize(p),
            "count": cnt,
        })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


def reparse(keep_state=True):
    doc = CFG["doc"]
    if not os.path.isfile(doc):
        raise RuntimeError("找不到文档：" + doc)
    new_shots = parse_doc(doc)          # 解析在锁外做（要读素材目录，慢）
    with WRLOCK:
        if keep_state:
            old = load_shots() or {}
            # 只有还是同一份文档才继承手动状态；换了文档，G01 已不是原来的 G01
            same_doc = (old.get("doc") or "") == doc
            oldmap = {s["id"]: s for s in (old.get("shots") or [])}
            if same_doc:
                for s in new_shots:
                    o = oldmap.get(s["id"])
                    if not o:
                        continue
                    # 内容守卫：id 相同但正文不同，说明是另一份文档的同一个段号，
                    # 绝对不能继承这里的「已生成 / 出片路径」，否则点看片会播旧文档的视频
                    if (o.get("prompt") or "")[:60] != (s.get("prompt") or "")[:60]:
                        log("段 %s 内容与旧记录不同，按新文档处理（不继承出片状态）" % s["id"])
                        continue
                    # 这些是「后台状态 / 用户选择」，文档里没有，必须原样带过来
                    for k in ("status", "out", "skip", "seed", "note", "deleted",
                              "lang", "extra_texts", "ver", "pic_n", "vid_n",
                              "pic_more", "vid_more", "no_pic", "purged",
                              "edited", "edited_cn", "edited_pic"):
                        if k in o:
                            s[k] = o[k]
                    # 用户在 UI 里改过的提示词优先保留
                    if o.get("edited") and (o.get("prompt") or ""):
                        s["prompt"] = o["prompt"]
                    if o.get("edited_cn") and (o.get("prompt_cn") or ""):
                        s["prompt_cn"] = o["prompt_cn"]
                    # 图槽 1–9 全都要保（原来只保 1–3，图4~6 手选的会被文档覆盖）
                    if o.get("edited_pic"):
                        for i in range(1, 10):
                            k = "pic%d" % i
                            # 手选过：保留用户的选择；但旧选择是「空」而这次匹配上了 → 以匹配为准
                            if k in o and (o[k] or not (s.get(k) or "")):
                                s[k] = o[k]
                            kr = "raw%d" % i
                            if not (s.get(kr) or "") and o.get(kr):
                                s[kr] = o[kr]
                        for i in range(1, 6):
                            k = "vid%d" % i
                            if k in o and (o[k] or not (s.get(k) or "")):
                                s[k] = o[k]
            else:
                log("检测到换了文档，全部按新文档重建（旧文档的状态不带入）")
            # 手动新增的段在文档里没有对应标题，重新解析时挂到末尾保留下来
            have = {s["id"] for s in new_shots}
            keep_extra = [o for o in (old.get("shots") or [])
                          if same_doc and o.get("id") not in have
                          and "手动新增" in (o.get("note") or "")]
            if keep_extra:
                new_shots.extend(keep_extra)
                log("重新解析：保留 %d 段手动新增的段（%s）"
                    % (len(keep_extra), "、".join(str(x.get("id")) for x in keep_extra)))
        save_json(SHOTS_PATH, {"doc": CFG["doc"], "assets": CFG["assets"], "shots": new_shots})
    alive = [s for s in new_shots if not s.get("deleted")]
    miss = [s["id"] for s in alive if not s.get("pic1") and not s.get("skip") and not s.get("no_pic")]
    log(f"解析完成：{len(alive)} 段，素材库 {len(list_assets())} 张"
        + (f"，{len(miss)} 段缺图1（{','.join(miss)}）" if miss else "，全部图槽已匹配")
        + ((f"（另有 {len(new_shots) - len(alive)} 段是已删除的，没算进来）")
           if len(new_shots) > len(alive) else ""))
    return new_shots


# ---------------------------------------------------------------- ComfyUI
def comfy_get(path, timeout=10):
    with _OPENER.open(CFG["comfy"] + path, timeout=timeout) as r:
        return json.load(r)


def comfy_post(path, obj, timeout=30):
    data = json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(CFG["comfy"] + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with _OPENER.open(req, timeout=timeout) as r:
        return json.load(r)


_COMFY_ON = {"t": 0.0, "v": False}


def comfy_online(max_age=0.0):
    """探 ComfyUI 是否在线。max_age>0 时结果缓存这么多秒（前端 2.5 秒轮询一次，
    每次都真探一遍会把 ComfyUI 拖慢，也没意义）。"""
    now = time.time()
    if max_age and _COMFY_ON["t"] and now - _COMFY_ON["t"] < max_age:
        return _COMFY_ON["v"]
    try:
        comfy_get("/system_stats", timeout=3)
        _COMFY_ON.update(t=now, v=True)
        return True
    except Exception:
        _COMFY_ON.update(t=now, v=False)
        return False


def _interrupt_comfy():
    """
    真正让 ComfyUI 停下来：/interrupt 打断正在跑的那一段，
    再把队列里「本工具提交、还没开始」的任务删掉。
    （以前只置了个 STOP 标记，日志写着「已停止」，显卡其实还在跑。）
    """
    try:
        comfy_post("/interrupt", {}, timeout=10)
        log("已通知 ComfyUI 中断当前任务")
    except Exception as e:
        log("通知 ComfyUI 中断失败（可能这一段已经跑完了）：" + str(e)[:120])
    try:
        q = comfy_get("/queue", timeout=10)
        pend = q.get("queue_pending") or []
        mine = [it[1] for it in pend if len(it) > 3 and _is_ours(it[3])]
        if mine:
            comfy_post("/queue", {"delete": mine}, timeout=10)
            log("已从 ComfyUI 队列撤掉 %d 个还没开始的任务" % len(mine))
    except Exception:
        pass


def _pick_ps(mode="file", title="", initial="", kinds="", defname="", ext="mp4"):
    """
    备用方案：用 Windows 自带的 PowerShell + WinForms 弹选择框
    （适用于精简版 python，比如没带 tkinter 的嵌入式发行版）。
    返回选中的路径；用户取消返回空串。
    """
    def ps(s):
        return "'" + str(s or "").replace("'", "''") + "'"

    lines = [
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8",
        "Add-Type -AssemblyName System.Windows.Forms | Out-Null",
        "Add-Type -AssemblyName System.Drawing | Out-Null",
        # 一个屏幕外的置顶隐形窗口当 owner，保证对话框弹在最前面
        "$o = New-Object System.Windows.Forms.Form",
        "$o.TopMost = $true",
        "$o.ShowInTaskbar = $false",
        "$o.Opacity = 0",
        "$o.StartPosition = 'Manual'",
        "$o.Location = New-Object System.Drawing.Point(-3000, -3000)",
        "$o.Size = New-Object System.Drawing.Size(1, 1)",
        "$o.Show()",
    ]
    if mode == "dir":
        lines += [
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog",
            "$d.Description = " + ps(title or "选择文件夹"),
            "$d.ShowNewFolderButton = $true",
        ]
        if initial and os.path.isdir(initial):
            lines.append("$d.SelectedPath = " + ps(initial))
        lines.append("$r = $d.ShowDialog($o)")
        lines.append("if ($r -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Write($d.SelectedPath) }")
    else:
        cls = "SaveFileDialog" if mode == "save" else "OpenFileDialog"
        e = (ext or "mp4").strip().lstrip(".")
        lines += [
            "$d = New-Object System.Windows.Forms." + cls,
            "$d.Title = " + ps(title or ("另存为" if mode == "save" else "选择文件")),
        ]
        if mode == "save":
            _e0 = e.lower()
            _lab0 = ("Markdown 文档" if _e0 in ("md", "markdown")
                     else ("文本文档" if _e0 == "txt" else "媒体文件"))
            pairs = [(_lab0, "*." + e), ("所有文件", "*.*")]
            lines.append("$d.DefaultExt = " + ps(e))
            lines.append("$d.OverwritePrompt = $true")
            if defname:
                lines.append("$d.FileName = " + ps(defname))
        elif kinds == "md":
            pairs = [("Markdown 文档", "*.md"), ("文本文件", "*.md;*.markdown;*.txt"), ("所有文件", "*.*")]
        else:
            pairs = [("所有文件", "*.*")]
        lines.append("$d.Filter = " + ps("|".join("%s (%s)|%s" % (n, p, p) for n, p in pairs)))
        lines.append("$d.CheckFileExists = $" + ("false" if mode == "save" else "true"))
        if initial:
            if os.path.isdir(initial):
                lines.append("$d.InitialDirectory = " + ps(initial))
            elif os.path.isfile(initial):
                lines.append("$d.InitialDirectory = " + ps(os.path.dirname(initial)))
                if mode != "save":
                    lines.append("$d.FileName = " + ps(os.path.basename(initial)))
        lines.append("$r = $d.ShowDialog($o)")
        lines.append("if ($r -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Write($d.FileName) }")
    lines += ["$o.Close()", "$o.Dispose()"]

    out = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", "\n".join(lines) + "\n"],
                         capture_output=True, encoding="utf-8", errors="replace", timeout=90)
    p = (out.stdout or "").strip()
    if not p and out.stderr and out.stderr.strip():
        raise RuntimeError(out.stderr.strip().splitlines()[-1][:200])
    return p


def _pick_tk(mode="file", title="", initial="", kinds="", defname="", ext="mp4"):
    """首选方案：tkinter 原生对话框（python.org 装的 python 都带）。"""
    lines = [
        "import sys",
        "try:",
        "    sys.stdout.reconfigure(encoding='utf-8')",
        "except Exception:",
        "    pass",
        "import os, ctypes, threading, time, tkinter as tk",
        "from tkinter import filedialog",
        # 关键：主窗口不能 withdraw。一个隐藏窗口没有前台权，系统对话框会弹到
        # 浏览器后面去（用户得最小化网页才看得见）。留一个 1x1 的置顶窗口当 owner，
        # 对话框就是它的子窗口，自然跟到最前面。
        "u=ctypes.windll.user32",
        "try: u.AllowSetForegroundWindow(-1)",
        "except Exception: pass",
        "r=tk.Tk()",
        "r.overrideredirect(True)",
        "r.geometry('1x1+60+60')",
        "r.attributes('-topmost', True)",
        "r.attributes('-alpha', 0.01)",
        "r.update()",
        "_T=%s" % ascii(title or ""),
        "def _fg():",
        "    for _ in range(80):",
        "        try:",
        "            h=u.FindWindowW('#32770', _T) if _T else u.FindWindowW('#32770', None)",
        "        except Exception:",
        "            h=0",
        "        if h:",
        "            try:",
        "                u.SetWindowPos(h, -1, 0, 0, 0, 0, 0x0001 | 0x0002)",
        "                u.SetForegroundWindow(h)",
        "                u.SetFocus(h)",
        "            except Exception:",
        "                pass",
        "            break",
        "        time.sleep(0.05)",
        "threading.Thread(target=_fg, daemon=True).start()",
        "kw={'title': %s}" % ascii(title),
        "kw['parent']=r",
        "init=%s" % ascii(initial),
        "if init and os.path.isdir(init):",
        "    kw['initialdir']=init",
        "elif init and os.path.isfile(init):",
        "    kw['initialdir']=os.path.dirname(init)",
        "    kw['initialfile']=os.path.basename(init)",
    ]
    if mode == "save":
        e2 = (ext or "mp4").strip().lstrip(".")
        lines.append("kw['defaultextension']=" + ascii("." + e2))
        _e = e2.lower()
        _lab = ("Markdown 文档" if _e in ("md", "markdown")
                else ("文本文档" if _e == "txt" else "媒体文件"))
        lines.append("kw['filetypes']=[(" + ascii(_lab) + "," + ascii("*." + e2) + "),('所有文件','*.*')]")
        if defname:
            lines.append("kw['initialfile']=" + ascii(defname))
        lines.append("p=filedialog.asksaveasfilename(**kw)")
    elif mode == "file":
        if kinds == "md":
            lines.append("kw['filetypes']=[('Markdown 文档','*.md'),"
                         "('文本','*.md *.markdown *.txt'),('所有文件','*.*')]")
        else:
            lines.append("kw['filetypes']=[('所有文件','*.*')]")
        lines.append("p=filedialog.askopenfilename(**kw)")
    else:
        lines.append("p=filedialog.askdirectory(**kw)")
    lines += ["try:", "    r.destroy()", "except Exception:", "    pass", "print(p or '')"]

    out = subprocess.run([sys.executable, "-c", "\n".join(lines) + "\n"],
                         capture_output=True, encoding="utf-8", errors="replace", timeout=90)
    err = (out.stderr or "")
    if "tkinter" in err.lower() or "no module named" in err.lower():
        raise ModuleNotFoundError("tkinter 不可用")
    if out.returncode != 0:
        raise RuntimeError(err.strip().splitlines()[-1][:200] if err.strip() else "对话框异常退出")
    rows = [x for x in (out.stdout or "").strip().splitlines() if x.strip()]
    return rows[-1].strip() if rows else ""


def pick_dialog(mode="file", title="", initial="", kinds="", defname="", ext="mp4"):
    """
    弹系统选择框。先试 tkinter（快、原生），这套 python 没带 tkinter 就退回 PowerShell。
    返回选中的路径；用户取消返回空串。
    """
    try:
        return _pick_tk(mode, title, initial, kinds, defname, ext)
    except subprocess.TimeoutExpired:
        # 子进程 90 秒还没结果：对话框多半弹在了看不见的会话里。
        # 这时再退到另一个备选方案，只会再弹一个同样看不见的窗口，不如直接说清楚。
        raise RuntimeError("系统对话框等了 90 秒没有反应。\n"
                           "请自己在桌面上双击「启动文档导演台.bat」重新启动，再试一次。")
    except Exception:
        return _pick_ps(mode, title, initial, kinds, defname, ext)


_OBJINFO = {"t": 0.0, "d": None}


def comfy_object_info(force=False):
    """ComfyUI 的 /object_info：每个节点类的参数定义（下拉框的候选项就在这里面）。"""
    import time as _time
    now = _time.time()
    if not force and _OBJINFO["d"] and now - _OBJINFO["t"] < 600:
        return _OBJINFO["d"]
    try:
        d = comfy_get("/object_info", timeout=20)
        _OBJINFO.update(t=now, d=d)
        return d
    except Exception:
        return _OBJINFO["d"] or {}


def combo_options(cls, key):
    """某节点类某 input 的下拉候选项；不是下拉就返回 None。
    兼容两种格式：旧版 [["a","b"],{}] 与新版 ["COMBO",{"options":["a","b"],...}]。"""
    info = comfy_object_info().get(cls) or {}
    for sect in ("required", "optional"):
        d0 = ((info.get("input") or {}).get(sect) or {}).get(key)
        if not isinstance(d0, list) or not d0:
            continue
        opts = None
        if isinstance(d0[0], list):
            opts = [str(x) for x in d0[0] if not isinstance(x, (list, dict))]
        elif d0[0] == "COMBO":
            cfg = d0[1] if len(d0) > 1 and isinstance(d0[1], dict) else {}
            o = cfg.get("options")
            if isinstance(o, list) and o:
                opts = [str(x) for x in o if not isinstance(x, (list, dict))]
        if opts:
            return opts
    return None


# ---------------------------------------------------------------- 进度通道
def on_ws_msg(msg):
    """处理 ComfyUI 的 ws 事件，更新当前进度。"""
    t = msg.get("type")
    d = msg.get("data") or {}
    # 只跟自己提交的那个 prompt，别把别人在 ComfyUI 里跑的任务算进来
    pid = d.get("prompt_id")
    cur = STATE.get("cur_pid")
    if cur and pid and pid != cur:
        return
    p = dict(STATE.get("progress") or {})
    if t == "execution_start":
        STATE["progress"] = {"node": "", "value": 0, "max": 0,
                             "t0": time.time(), "prompt_id": d.get("prompt_id", "")}
    elif t == "executing":
        node = d.get("node")
        if node:
            p["node"] = str(node)
            STATE["progress"] = p
        else:
            STATE["progress"] = {"node": "", "value": 0, "max": 0, "t0": 0}
    elif t == "progress":
        p["value"] = int(d.get("value") or 0)
        p["max"] = int(d.get("max") or 0)
        STATE["progress"] = p
    elif t == "execution_error":
        STATE["progress"] = {"node": "", "value": 0, "max": 0, "t0": 0,
                             "error": str(d.get("exception_message") or "")[:200]}
    elif t == "status":
        q = ((d.get("status") or {}).get("exec_info") or {}).get("queue_remaining")
        if q is not None:
            STATE["queue_remaining"] = int(q)


def start_ws():
    global WS
    if WS is not None:
        return WS
    try:
        WS = comfy_ws.ComfyWS(CFG["comfy"], CID, on_ws_msg, logger=log)
        WS.start()
        log("进度通道已启动（能看到当前跑到哪个节点、第几/共几）")
    except Exception as e:
        log("进度通道启动失败：" + str(e))
    return WS


def build_graph(template, shot, pic_names, vid_names=None):
    """把一镜的值注进工作流副本。节点位置全部来自 workflow_map.json（可换工作流）。"""
    g = copy.deepcopy(template)
    m = get_mapping()
    s = dict(shot)
    s["_project"] = CFG["project_name"]
    # 语言选择：cn 就用文档里的【中文版】喂模型，en 用【英文版】
    if (s.get("lang") or "en") == "cn":
        s["prompt"] = s.get("prompt_cn") or s.get("prompt") or ""
    wfmap.apply(g, s, m, pic_names, vid_names or {})

    # --- 提示词必须落到最终消费节点（字面量）---
    # H3 的 261 是唯一真正吃提示词的节点。上游那些 PrimitiveString / 字符串拼接
    # 改了值也不会让下游重算，所以这里直接写死一份，绕开中间层。
    if "261" in g:
        txt = s.get("prompt") or s.get("prompt_cn") or ""
        if txt:
            g["261"].setdefault("inputs", {})["prompt"] = txt

    # --- 旧版 MiniMax H3 专属补丁：节点存在才生效，换别的流无感 ---
    if not pic_names.get(3) and "261" in g:
        g["261"]["inputs"].pop("ref_images.ref_image_2", None)
    proj = CFG["project_name"]
    sid = shot.get("id", "shot")
    if "5142" in g:
        g["5142"]["inputs"]["project_name"] = proj
        g["5142"]["inputs"]["mode"] = "Force Initial (强制首段，无上下文)"
    if "5143" in g:
        g["5143"]["inputs"]["project_name"] = proj
        g["5143"]["inputs"]["shot_tag"] = sid

    # --- 缓存保险丝 ---
    # Seed(rgthree) 这个节点的 IS_CHANGED 不随 seed 变化，ComfyUI 会把整条采样链
    # 判成「命中缓存」直接复用旧结果（表现为几秒就完成、播的还是上一个视频）。
    # 这里把种子直接写成采样器噪声节点的字面量，保证每次提交都必然重算。
    if "5367:5360" in g and s.get("seed"):
        g["5367:5360"]["inputs"]["noise_seed"] = int(s["seed"])

    if CFG.get("onepass"):
        for nid in TWOPASS_NODES:
            g.pop(nid, None)
        sav = g.get("5143")
        if sav:
            for k in ("latent_二采", "images_二采"):
                sav.get("inputs", {}).pop(k, None)
        for nid, node in list(g.items()):
            for k, v in list((node.get("inputs") or {}).items()):
                if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and v[0] not in g:
                    node["inputs"].pop(k, None)

    # 附加文本槽（比如「subject definitions / lora触发词」这种旁路文本节点）
    # 每段单独勾选要不要注入、注入什么；没勾的保持模板原值。
    ents = m.get("extra_texts") or []
    if ents:
        rec = shot.get("extra_texts") or {}
        for ent in ents:
            nid = str((ent or {}).get("node") or "")
            if not nid or nid not in g:
                continue
            r = rec.get(nid) or rec.get(str(nid)) or {}
            if not r.get("on"):
                continue
            g[nid]["inputs"][(ent or {}).get("input") or "value"] = str(r.get("text") or "")

    # 最后把整条没参与计算的链（没用到的参考图 + 它的 Resize）删干净，省显存
    try:
        wfmap.prune_graph(g)
    except Exception:
        pass
    return g


def _err_detail(rec):
    """从 ComfyUI 的 history 记录里把真正的报错原因挖出来。"""
    st = rec.get("status") or {}
    for m in (st.get("messages") or []):
        if not (isinstance(m, list) and len(m) >= 2):
            continue
        if m[0] == "execution_interrupted":
            return "被中断了（点了停止，或者任务在 ComfyUI 里被取消/删掉）"
        if m[0] != "execution_error":
            continue
        d = m[1] or {}
        et = d.get("exception_type") or ""
        msg = str(d.get("exception_message") or "").strip()
        node = ("%s %s" % (d.get("node_id") or "", d.get("node_type") or "")).strip()
        s = ((et + "：") if et else "") + msg
        if node:
            s += f"（出错节点 {node}）"
        low = (et + msg).lower()
        if "outofmemory" in low or "out of memory" in low:
            s += ("\n\n→ 显存不够。可以试试：① 把分辨率调小；② 这段少用几张参考图（图4/5/6 别都填）；"
                  "③ 时长改短；④ 在 ComfyUI 里确认二采/分块开关是不是比之前更吃显存了。")
        return s[:800]
    return ""


def wait_done(pid, timeout=7200):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if STOP.is_set():
            raise RuntimeError("已停止")
        try:
            h = comfy_get(f"/history/{pid}", timeout=10)
        except Exception:
            h = {}
        if pid in h:
            rec = h[pid]
            st = rec.get("status", {}) or {}
            if st.get("status_str") == "error":
                raise RuntimeError("ComfyUI 报错：" + (_err_detail(rec) or "未知错误（去 ComfyUI 控制台看）"))
            if st.get("completed"):
                return rec
        time.sleep(3)
    raise TimeoutError("单段超时")


def find_video(rec, sid, since=0):
    outs = rec.get("outputs", {}) or {}
    for nid in ("667", "665"):
        o = outs.get(nid, {}) or {}
        for key in ("gifs", "videos", "images"):
            for it in (o.get(key) or []):
                fn = it.get("filename", "")
                if fn.lower().endswith((".mp4", ".webm", ".mov", ".mkv")):
                    return os.path.join(CFG["comfy_output_dir"], it.get("subfolder", ""), fn)
    sub = CFG["project_name"]
    base = os.path.join(CFG["comfy_output_dir"], sub)
    # 兜底：按段号找最新落盘的那个。必须要求「段号后面跟着 - _ . 」，
    # 否则找 G1 的时候会把 G10、G100 的片也认成自己。
    # since：只接受这个时间点之后写盘的文件，否则会把上一次生成的老片当成这次的结果。
    pat = re.compile(r"^" + re.escape(str(sid)) + r"[-_.]", re.I)
    cands = sorted((p for p in glob.glob(os.path.join(base, str(sid) + "*"))
                    if pat.match(os.path.basename(p))
                    and p.lower().endswith((".mp4", ".webm", ".mov", ".mkv"))
                    and (not since or os.path.getmtime(p) >= since)),
                   key=os.path.getmtime)
    return cands[-1] if cands else ""


NEW_MARK = "（新）"


def _strip_new(name):
    return name.replace(NEW_MARK, "").replace("(新)", "")


def fix_new_marks(d, sid):
    """保证同一个段有且只有最新那个版本带（新）"""
    try:
        pat = re.compile(r"^" + re.escape(sid) + r"-\d+")
        cands = [fn for fn in os.listdir(d)
                 if pat.match(fn) and fn.lower().endswith((".mp4", ".mov", ".webm", ".mkv"))]
        if not cands:
            return
        cands.sort(key=lambda fn: os.path.getmtime(os.path.join(d, fn)), reverse=True)
        newest = cands[0]
        # 最新那个没标记 → 打上
        if NEW_MARK not in newest and "(新)" not in newest:
            stem, ext = os.path.splitext(os.path.join(d, newest))
            dst = stem + NEW_MARK + ext
            if not os.path.exists(dst):
                os.rename(os.path.join(d, newest), dst)
                cands[0] = os.path.basename(dst)
        # 其余带标记的去标记
        for fn in cands[1:]:
            if NEW_MARK not in fn:
                continue
            dst = os.path.join(d, _strip_new(fn))
            if not os.path.exists(dst):
                os.rename(os.path.join(d, fn), dst)
    except Exception:
        pass


def rename_versioned(path, shot_id, ver):
    """
    G01-2_00001-audio.mp4 → G01-2（新）.mp4
    同段其他版本统一去掉（新）—— 只有最新那版带标记。
    """
    try:
        if not path or not os.path.isfile(path):
            return path
        d = os.path.dirname(path)
        ext = os.path.splitext(path)[1] or ".mp4"
        target = os.path.join(d, "%s-%d%s%s" % (shot_id, ver, NEW_MARK, ext))
        if os.path.abspath(target) == os.path.abspath(path):
            return path
        n = 2
        while os.path.exists(target):
            target = os.path.join(d, "%s-%d(%d)%s%s" % (shot_id, ver, n, NEW_MARK, ext))
            n += 1
        os.rename(path, target)
        # VHS 会同时写一张同名缩略图（G01-2_00001.png），跟着一起改名，
        # 前端状态列的视频缩略图直接用它当封面，省得浏览器去解码视频首帧。
        try:
            stem = os.path.splitext(path)[0]
            for suf in ("-audio", "-video", ""):
                if suf and stem.endswith(suf):
                    stem = stem[: -len(suf)]
                    break
            png = stem + ".png"
            if os.path.isfile(png):
                os.replace(png, os.path.splitext(target)[0] + ".png")
        except Exception:
            pass
        fix_new_marks(d, shot_id)
        return target
    except Exception:
        return path


# ---------------------------------------------------------------- 成片删除
_CLIP_DUR = {}
_CLIPS_MAINT = {"t": 0.0}      # 上次「整理成片目录」的时间（迁移/补标记/修 out）


def clip_duration(path):
    """读成片时长（秒），按 文件+大小+修改时间 缓存"""
    try:
        st = os.stat(path)
        key = "%s|%d|%d" % (path, st.st_size, int(st.st_mtime))
    except Exception:
        return 0.0
    if key in _CLIP_DUR:
        return _CLIP_DUR[key]
    d = 0.0
    try:
        out = subprocess.check_output(
            [_bin("ffprobe"), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            stderr=subprocess.DEVNULL, timeout=20)
        d = float((out or b"0").decode().strip() or 0)
    except Exception:
        d = 0.0
    _CLIP_DUR[key] = round(d, 1)
    if len(_CLIP_DUR) > 4000:      # 键带文件大小和时间，只增不减，给个上限
        _CLIP_DUR.clear()
    return _CLIP_DUR[key]


def sync_outs(d):
    """shots.json 里指向已不存在文件的 out，按段号重新指到最新那个成片"""
    box = {"changed": False}
    _hid = hidden_set("clips")   # 被「从列表移除」的成片不参与回指，否则删了会复活

    def _do(data):
        changed = False
        for s in data.get("shots", []):
            p = s.get("out") or ""
            if p and os.path.isfile(p):
                continue
            sid = s.get("id") or ""
            if not sid:
                continue
            cands = sorted(glob.glob(os.path.join(d, sid + "-*.mp4")), key=os.path.getmtime)
            cands = [c for c in cands if os.path.basename(c) not in _hid]
            if cands:
                s["out"] = cands[-1]
                s["status"] = "done"
                changed = True
        box["changed"] = changed
        return changed

    mutate_shots(_do)
    return box["changed"]


def migrate_old_clips(d):
    """
    把旧的 G01_00002-audio.mp4 整理成 G01-2.mp4（最新的打上（新）），
    只动「有声版」，无声版留着不碰。
    """
    if not os.path.isdir(d):
        return 0
    groups = {}
    for fn in os.listdir(d):
        m = re.match(r"^(.*?)_(\d{5})-audio\.mp4$", fn, re.I)
        if m:
            groups.setdefault(m.group(1), []).append(fn)
    moved = 0
    for sid, files in groups.items():
        files.sort(key=lambda f: os.path.getmtime(os.path.join(d, f)))
        for i, fn in enumerate(files, start=1):
            target = os.path.join(d, "%s-%d.mp4" % (sid, i))
            if os.path.exists(target):
                continue
            try:
                os.rename(os.path.join(d, fn), target)
                moved += 1
            except Exception:
                pass
        try:
            fix_new_marks(d, sid)
        except Exception:
            pass
    if moved:
        log("旧成片已整理成新命名：%d 个" % moved)
        sync_outs(d)
    return moved


def cur_engine():
    """当前引擎。写错了就退回本地 ComfyUI，别让一个错字把整个工具卡死。"""
    e = str(CFG.get("engine") or "comfy").strip().lower()
    return e if (e == "comfy" or e in engines.CLOUD_ENGINES) else "comfy"


def doc_tag():
    """当前文档的短名，用作输出子目录，避免不同文档的 G01 撞在一起"""
    p = CFG.get("doc") or ""
    n = os.path.splitext(os.path.basename(p))[0]
    n = re.sub(r'[\\/:*?"<>|\s]+', "_", n).strip("_")
    return n[:40] or "未命名文档"


def out_root():
    """成片输出根目录：output/<project>/<文档名>/
    每份文档一个子目录，看片、删片、合成都只在自己目录里，不会串到别的文档。"""
    d = os.path.join(CFG["comfy_output_dir"], CFG["project_name"], doc_tag())
    os.makedirs(d, exist_ok=True)
    return d


def find_out_file(name):
    """按文件名找成片：先找当前文档的子目录，找不到再回退旧的共用目录（兼容老片）"""
    name = os.path.basename(name)
    p = os.path.join(out_root(), name)
    if os.path.isfile(p):
        return p
    old = os.path.join(CFG["comfy_output_dir"], CFG["project_name"], name)
    return old if os.path.isfile(old) else p


def cloud_outdir():
    """云端引擎把成片下载到这里，目录结构和 ComfyUI 的 output 保持一致，
    这样版本号、删除成片、合成整片全都不用改。"""
    return out_root()


def run_one(template, shot):
    # 版本号从 1 开始：G01-1、G01-2 ……能看出第几版，旧版也不覆盖
    ver = int(shot.get("ver", 0)) + 1
    shot["ver"] = ver
    shot["_ver"] = ver
    eng = cur_engine()

    # ---------- 云端引擎（可灵 / 即梦）----------
    # 不经过 ComfyUI：参考图直接用素材库原图，省掉「拷进 input 目录」那一步
    if eng in engines.CLOUD_ENGINES:
        srcs = {}
        for slot in range(1, 10):
            fn = shot.get("pic%d" % slot) or ""
            if not fn:
                continue
            p = os.path.join(CFG["assets"], fn)
            if os.path.exists(p):
                srcs[slot] = p
        if not srcs and not shot.get("no_pic"):
            raise RuntimeError("图1 未匹配到素材，无法生成"
                               "（如果这段本来就不需要参考图，在「新增一段」里勾上「不用参考图」）")
        bad = engines.check_shot(eng, shot, srcs)
        if bad:
            raise RuntimeError("；".join(bad))
        raw = engines.run(eng, shot, srcs, CFG, cloud_outdir(),
                          log=log, stop_check=STOP.is_set, ffmpeg_bin=_bin("ffmpeg"))
        return rename_versioned(raw, shot["id"], ver)

    # ---------- 本地 ComfyUI ----------
    pics, vids = {}, {}
    for slot in range(1, 10):
        fn = shot.get("pic%d" % slot) or ""
        if fn:
            src = os.path.join(CFG["assets"], fn)
            if os.path.exists(src):
                pics[slot] = safe_copy(src)
    for slot in range(1, 6):
        fn = shot.get("vid%d" % slot) or ""
        if fn:
            src = os.path.join(CFG["assets"], fn)
            if os.path.exists(src):
                vids[slot] = safe_copy(src)
    if get_mapping().get("images") and 1 not in pics and not shot.get("no_pic"):
        raise RuntimeError("图1 未匹配到素材，无法生成"
                           "（如果这段本来就不需要参考图，在「新增一段」里勾上「不用参考图」）")
    # 每次给一个不同的种子。ComfyUI 的子图缓存只认子图自己的输入：提示词变了
    # 不一定会让子图内部的采样重算，但种子变了一定会。用户固定了种子就尊重他。
    if not shot.get("seed"):
        shot["seed"] = random.randint(1, 2147483647)
    g = build_graph(template, shot, pics, vids)
    t0 = time.time() - 5          # 允许几秒误差：只认这个时间之后落盘的片
    resp = comfy_post("/prompt", {"prompt": g, "client_id": CID})
    pid = resp.get("prompt_id")
    if not pid:
        raise RuntimeError("提交失败: " + json.dumps(resp, ensure_ascii=False)[:300])
    log(f"{shot['id']} 已提交 {pid[:8]}…")
    STATE["cur_pid"] = pid
    rec = wait_done(pid)
    path = find_video(rec, shot["id"], since=t0)
    if not path:
        # 没找到新文件，按真实原因报错，别一律说成「命中缓存」
        msgs = ((rec.get("status") or {}).get("messages") or [])
        types = [m[0] for m in msgs if m]
        out_keys = list((rec.get("outputs") or {}).keys())
        if "execution_interrupted" in types:
            raise RuntimeError(
                "ComfyUI 执行被中断了（不是报错）。常见原因：在 ComfyUI 界面点了停止/清理队列、"
                "或者别的客户端连着同一个 ComfyUI。确认那边没有操作后再试。")
        if "execution_error" in types:
            raise RuntimeError("ComfyUI 执行出错：" + (_err_detail(rec) or "看 ComfyUI 控制台"))
        if "execution_cached" in types and "665" not in out_keys:
            raise RuntimeError(
                "ComfyUI 命中了缓存，这次没有真正生成视频（几秒就结束、播的还是旧片就是这个原因）。"
                "去 ComfyUI 界面点一下「Clear Cache」，或在导演台把种子改一个再跑。")
        raise RuntimeError("没找到本次生成的视频文件（输出节点可能被禁用，或存到了别的目录）")
    path = rename_versioned(path, shot["id"], ver)
    return path


def _unique_dest(dest, base):
    """目标目录里已有同名文件时自动加 (2)(3)，不再静默覆盖别人导过的片。"""
    p = os.path.join(dest, base)
    if not os.path.exists(p):
        return p
    stem, ext = os.path.splitext(base)
    n = 2
    while os.path.exists(os.path.join(dest, "%s(%d)%s" % (stem, n, ext))):
        n += 1
    return os.path.join(dest, "%s(%d)%s" % (stem, n, ext))


def do_export(files, dest):
    """后台逐个复制成片，进度写到 STATE["export"]，前端轮询显示。"""
    outdir = out_root()
    with LOCK:
        STATE["export"] = {"running": True, "total": len(files), "done": 0,
                           "current": "", "copied": 0, "missing": 0, "failed": 0,
                           "error": "", "dest": dest}
    copied, missing, failed = 0, 0, 0
    moves = {}          # 原绝对路径 → 新绝对路径，最后统一改 shots 里的 out
    try:
        for i, fn in enumerate(files, 1):
            with LOCK:
                STATE["export"]["current"] = fn
            src = os.path.join(outdir, fn)
            if not os.path.isfile(src):
                missing += 1
                log("导出：源文件不在了 → %s" % fn)
            else:
                base = _strip_new(fn)
                try:
                    dst = _unique_dest(dest, base)
                    # 直接移动：原位置不再保留（不是复制）
                    shutil.move(src, dst)
                    moves[os.path.abspath(src)] = os.path.abspath(dst)
                    copied += 1
                except Exception as e:
                    # 移动失败和「源文件没找到」是两回事，分开统计，提示才不误导
                    failed += 1
                    log("导出失败（移动出错）：%s（%s）" % (fn, e))
            with LOCK:
                STATE["export"].update(done=i, copied=copied, missing=missing, failed=failed)
        # 文件搬走了，段里记的 out 也要跟着改，否则「完成」状态会指向不存在的路径
        if moves:
            def _repoint(d):
                for x in d.get("shots", []):
                    oldp = (x.get("out") or "").strip()
                    if oldp and os.path.abspath(oldp) in moves:
                        x["out"] = moves[os.path.abspath(oldp)]
                return True
            mutate_shots(_repoint)
        log("导出完成：移动 %d 个 → %s%s%s" % (
            copied, dest,
            ("，%d 个移动失败" % failed) if failed else "",
            ("，%d 个源文件没找到" % missing) if missing else ""))
    except Exception as e:
        with LOCK:
            STATE["export"]["error"] = str(e)
        log("导出失败：" + str(e))
    finally:
        with LOCK:
            STATE["export"]["running"] = False
            STATE["export"]["current"] = ""


RESIDUE_RE = re.compile(r"_\d{5}")


def list_residue():
    """
    ComfyUI 直接落盘的原始文件（G01_00001.mp4 这种无声版 / 重命名前的残留）。
    导出列表按 _\\d{5} 过滤掉了，界面上看不到、也删不掉，白占空间。
    这里把它们列出来，供「清理残留文件」用；正在被某段当 out 用的不算残留。
    """
    d = out_root()
    if not os.path.isdir(d):
        return []
    used = set()
    for s in ((load_shots() or {}).get("shots") or []):
        p = (s.get("out") or "").strip()
        if p:
            used.add(os.path.abspath(p).lower())
    out = []
    for fn in os.listdir(d):
        if not fn.lower().endswith(VID_EXT) or not RESIDUE_RE.search(fn):
            continue
        p = os.path.join(d, fn)
        if not os.path.isfile(p) or os.path.abspath(p).lower() in used:
            continue
        out.append({"file": fn, "size": os.path.getsize(p),
                    "mtime": int(os.path.getmtime(p))})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def prune_baks(keep=5, keep_bad=5):
    """_bak 里的快照只增不减。留最近 N 份就够（坏文件备份单独留 N 份）。"""
    d = os.path.join(HERE, "_bak")
    if not os.path.isdir(d):
        return 0
    plain, bad = [], []
    for fn in os.listdir(d):
        if not fn.lower().endswith(".json") or not fn.startswith("shots"):
            continue
        p = os.path.join(d, fn)
        try:
            mt = os.path.getmtime(p)
        except Exception:
            continue
        (bad if ".bad-" in fn else plain).append((mt, p))
    n = 0
    for lst, lim in ((plain, keep), (bad, keep_bad)):
        lst.sort(reverse=True)
        for _mt, p in lst[lim:]:
            try:
                os.remove(p)
                n += 1
            except Exception:
                pass
    if n:
        log("_bak 清理：删掉 %d 份过旧快照（各留最近 %d / %d 份）" % (n, keep, keep_bad))
    return n


def _patch_shot(sid, **fields):
    """只改某一段的指定字段，其它段（和别的字段）保持文件里的最新值。"""
    with WRLOCK:
        d = load_shots() or {"shots": []}
        for x in d.get("shots", []):
            if x.get("id") == sid:
                x.update(fields)
                break
        save_json(SHOTS_PATH, d)
        return d


def mutate_shots(fn):
    """
    在写锁里「读 shots.json → 交给 fn 改 → 写回」。
    fn(data) 返回 False 表示不改（不写盘）。返回 (data, fn 的返回值)。
    """
    with WRLOCK:
        d = load_shots() or {"shots": []}
        r = fn(d)
        if r is not False:
            save_json(SHOTS_PATH, d)
        return d, r


def precheck_shot(s):
    """生成前的本地检查，返回问题列表（空 = 通过）。"""
    why = []
    if not (s.get("prompt") or "").strip():
        why.append("正面提示词为空")
    try:
        if float(s.get("sec") or 0) <= 0:
            why.append("时长必须大于 0")
    except Exception:
        why.append("时长不是数字")
    if s.get("no_pic"):
        if not (s.get("prompt") or "").strip():
            why.append("不用参考图时必须写提示词")
    elif not (s.get("pic1") or "").strip():
        why.append("图1 未选（确实不需要图就勾「不用参考图」）")
    # 云端引擎的硬限制在这里就拦：批量跑到一半才报超时长，前面的钱已经花掉了
    _lim = engines.LIMITS.get(cur_engine())
    if _lim:
        _label = engines.engine_label(cur_engine())
        try:
            _sec = float(s.get("sec") or 0)
        except Exception:
            _sec = 0.0
        if _sec and _sec > _lim["max_sec"]:
            why.append("时长 %.0f 秒超过%s上限 %d 秒（%s），这段要拆成两段或改短"
                       % (_sec, _label, _lim["max_sec"], _lim["durations"]))
        if _sec and _sec < _lim["min_sec"]:
            why.append("时长 %.0f 秒低于%s下限 %d 秒" % (_sec, _label, _lim["min_sec"]))
        _n = sum(1 for _i in range(1, 10) if (s.get("pic%d" % _i) or "").strip())
        if _n > _lim["max_images"]:
            why.append("参考图 %d 张超过%s上限 %d 张" % (_n, _label, _lim["max_images"]))
    return why


def run_indices(indices, bulk=False):
    data = load_shots() or {"shots": []}
    shots = data["shots"]
    sids = []
    for i in indices:
        if 0 <= i < len(shots):
            sids.append(shots[i].get("id"))
    tpl = load_json(os.path.join(HERE, CFG["template"]))
    try:
        retries = max(0, min(5, int(CFG.get("auto_retry") or 0)))
    except Exception:
        retries = 0
    # 云端引擎花钱是按秒走的：这一批要花多少，开跑前先算给你看，超上限就不启动。
    # 放在 running=True 之前，拦下来时状态是干净的。
    _eng = cur_engine()
    if bulk and _eng in engines.CLOUD_ENGINES:
        _secs = 0.0
        for _sid in sids:
            _s = next((x for x in shots if x.get("id") == _sid), None)
            if not _s or _s.get("skip") or _s.get("deleted"):
                continue
            try:
                _secs += float(_s.get("sec") or 0)
            except Exception:
                pass
        _conf = CFG.get(_eng) or {}
        _usd = engines.estimate_usd(_eng, _secs, _conf.get("mode", "pro"), _conf.get("sound", False))
        try:
            _budget = float(CFG.get("budget_usd") or 0)
        except Exception:
            _budget = 0.0
        log("=== 本批 %d 段 · %.0f 秒 · %s 预计约 $%.1f（¥%.0f）==="
            % (len(sids), _secs, engines.engine_label(_eng), _usd, _usd * 7.2))
        if _budget > 0 and _usd > _budget:
            log("!! 预计花费超过你设的上限 $%.1f，本次没有开跑。" % _budget
                + "要么调高 budget_usd，要么先只勾几段跑。")
            with LOCK:
                STATE["last_error"] = "预计 $%.1f 超过花费上限 $%.1f，已拦下" % (_usd, _budget)
            return
    with LOCK:
        STATE["running"] = True
        STATE["current"] = None
        STATE["last_error"] = ""
    STOP.clear()
    ok_n = fail_n = 0
    last_err = ""
    try:
        for sid in sids:
            if STOP.is_set():
                log("已停止")
                break
            # 每次开始前重读文件：生成过程中改过的提示词/图槽，下一段就按新的跑
            fresh = load_shots() or {"shots": []}
            shot = next((x for x in (fresh.get("shots") or []) if x.get("id") == sid), None)
            if shot is None or shot.get("deleted"):
                continue
            if shot.get("skip"):
                _patch_shot(sid, status="skip")
                continue
            bad = precheck_shot(shot)
            if bad and bulk:
                _patch_shot(sid, status="error", err="预检未通过：" + "；".join(bad))
                log("%s 预检未通过，已跳过：%s" % (sid, "；".join(bad)))
                fail_n += 1
                continue
            with LOCK:
                STATE["current"] = sid
            _patch_shot(sid, status="running", err="")
            path, err = None, ""
            tries = retries + 1
            # 重试时沿用同一个版本号：失败的那次不会占掉 G01-1，重试出来的还是 G01-1，
            # 不会出现「只有 G01-2，没有 G01-1」这种跳号
            base_ver = int(shot.get("ver") or 0)
            for attempt in range(1, tries + 1):
                shot["ver"] = base_ver
                shot["_ver"] = base_ver + 1
                try:
                    path = run_one(tpl, shot)
                    err = ""
                    break
                except Exception as e:
                    err = str(e)[:900]
                    if attempt < tries:
                        log("%s 第 %d 次失败，%d 秒后重试：%s"
                            % (sid, attempt, 2, err.splitlines()[0][:80] if err else ""))
                        time.sleep(2)
            if err:
                if err.startswith("已停止"):
                    # 手动停止不算失败：回到待生成，别在表格里留个红叉
                    _patch_shot(sid, status="pending", err="")
                    log("%s 已停止（这段回到待生成）" % sid)
                    break
                with LOCK:
                    STATE["last_error"] = "%s: %s" % (sid, err)
                _patch_shot(sid, status="error", err=err)
                log("%s 失败: %s" % (sid, err))
                fail_n += 1
                last_err = "%s: %s" % (sid, err)
                if not bulk:
                    break
                # 批量时接着跑后面的段：一段炸了不该把剩下 19 段一起停掉
                continue
            _patch_shot(sid, status="done", out=path, err="", ver=shot.get("ver"))
            log("%s 完成 → %s" % (sid, path or "未找到 mp4"))
            ok_n += 1
        if bulk and (fail_n or ok_n):
            log("批量结束：成功 %d 段%s" % (ok_n, ("，失败 %d 段（最后一条：%s）"
                                                % (fail_n, last_err.splitlines()[0][:120]))
                                            if fail_n else ""))
    finally:
        with LOCK:
            STATE["running"] = False
            STATE["current"] = None


# ---------------------------------------------------------------- 合成
def ffprobe_wh(path):
    try:
        out = subprocess.check_output([_bin("ffprobe"), "-v", "error", "-select_streams", "v:0",
                                       "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
                                      stderr=subprocess.DEVNULL).decode().strip()
        w, h = out.split(",")[:2]
        return int(w), int(h)
    except Exception:
        return 1312, 736


def _probe_sec(path):
    """读一个媒体的时长（秒）。读不出返回 0。"""
    try:
        out = subprocess.check_output(
            [_bin("ffprobe"), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            stderr=subprocess.DEVNULL, timeout=30)
        return float((out or b"0").decode().strip() or 0)
    except Exception:
        return 0.0


def normalize_clip(src, dst, w, h, sec, fps=24):
    """
    统一分辨率/帧率，并裁到文档时长。
    源片段比文档时长短时不再「默默变短」：补定格末帧把画面撑到目标时长，
    音轨用 apad 补静音。返回 (目标秒数, 源秒数, 补的秒数)。
    """
    sec = float(sec or 0)
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}")
    src_sec = _probe_sec(src)
    pad = 0.0
    if src_sec > 0 and sec > 0 and src_sec + 0.15 < sec:
        pad = sec - src_sec
        vf += f",tpad=stop_mode=clone:stop_duration={pad:.3f}"
    has_audio = True
    try:
        p = subprocess.run([_bin("ffprobe"), "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                            "-of", "csv=p=0", src], capture_output=True)
        has_audio = bool(p.stdout.strip())
    except Exception:
        pass
    if has_audio:
        cmd = [_bin("ffmpeg"), "-y", "-i", src, "-t", str(sec), "-vf", vf,
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
               "-af", "apad", "-c:a", "aac", "-ar", "44100", "-ac", "2", "-shortest", dst]
    else:
        cmd = [_bin("ffmpeg"), "-y", "-i", src, "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
               "-t", str(sec), "-vf", vf, "-map", "0:v:0", "-map", "1:a:0",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-ar", "44100", "-ac", "2", "-shortest", dst]
    subprocess.run(cmd, check=True)
    return sec, src_sec, pad


def make_black(dst, w, h, sec, fps=24):
    cmd = [_bin("ffmpeg"), "-y", "-f", "lavfi", "-i", f"color=c=black:s={w}x{h}:r={fps}:d={sec}",
           "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", str(sec),
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-ar", "44100", "-ac", "2", "-shortest", dst]
    subprocess.run(cmd, check=True)


def assemble(ids=None, dest=None):
    data = load_shots() or {"shots": []}
    shots = data["shots"]
    if ids:
        want = set(ids)
        shots = [x for x in shots if x.get("id") in want]
    outdir = out_root()
    os.makedirs(outdir, exist_ok=True)
    tmp = os.path.join(outdir, "_norm")
    os.makedirs(tmp, exist_ok=True)
    w = h = None
    for s in shots:
        p = s.get("out") or ""
        if p and os.path.exists(p):
            w, h = ffprobe_wh(p)
            break
    if not w:
        w, h = 1312, 736
    log(f"合成：分辨率 {w}x{h}")
    files = []
    todo = [x for x in shots if not x.get("deleted")]
    _total, _done = len(todo), 0
    with LOCK:
        STATE["asm_prog"] = {"done": 0, "total": _total, "cur": "", "phase": "归一化", "t0": time.time()}
    for i, s in enumerate(shots):
        if s.get("deleted"):
            continue
        with LOCK:
            STATE["asm_prog"]["cur"] = s.get("id") or ""
        dst = os.path.join(tmp, f"{i:02d}.mp4")
        p = s.get("out") or ""
        if p and os.path.exists(p):
            tgt, src_sec, pad = normalize_clip(p, dst, w, h, s["sec"])
            files.append(dst)
            if pad > 0.15:
                log("  %s 归一化（源片 %.1fs < 文档 %.1fs，已定格末帧补 %.1fs）"
                    % (s.get("id"), src_sec, tgt, pad))
            else:
                log(f"  {s.get('id')} 归一化")
        else:
            make_black(dst, w, h, s["sec"])
            files.append(dst)
            log(f"  {s.get('id')} 黑场 {s['sec']}s（未生成）")
        _done += 1
        with LOCK:
            STATE["asm_prog"]["done"] = _done
    if not files:
        raise RuntimeError("没有可合成的段落（都被删除了？）")
    lst = os.path.join(tmp, "list.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for n in files:
            f.write("file '" + n.replace("\\", "/") + "'\n")
    # 成片放哪儿：界面上选了文件夹就放那儿，没选就还是输出目录下的 doc_director
    final_dir = outdir
    if dest:
        final_dir = dest
        try:
            os.makedirs(final_dir, exist_ok=True)
        except Exception as e:
            raise RuntimeError("建不了这个文件夹：" + str(e))
    final = os.path.join(final_dir, ("成片_全片.mp4" if ids is None else "成片_自选.mp4"))
    had_old = os.path.isfile(final)
    with LOCK:
        STATE["asm_prog"].update({"phase": "拼接", "cur": "", "done": _total})
    subprocess.run([_bin("ffmpeg"), "-y", "-f", "concat", "-safe", "0", "-i", lst,
                    "-c", "copy", "-movflags", "+faststart", final], check=True)
    total = clip_duration(final)
    with LOCK:
        STATE["last_film"] = final
        STATE["asm_prog"].update({"phase": "完成", "cur": "", "done": _total})
    log("整片完成 → %s（%d 段 · %s）%s"
        % (final, len(files), ("%.1f 秒" % total) if total else "时长读不出",
           "（覆盖了同名旧文件）" if had_old else ""))
    return final


def assemble_job(ids=None, dest=None):
    """后台合成线程的入口：异常必须有人接，否则 ffmpeg 报错只留一行 traceback，
    界面上什么都看不到。"""
    with LOCK:
        STATE["last_film"] = ""      # 清掉上一次的结果，前端据此判断「这次」完成了
    try:
        assemble(ids, dest)
    except Exception as e:
        msg = "合成失败：" + str(e)[:400]
        with LOCK:
            STATE["last_error"] = msg
        log(msg)
    finally:
        with LOCK:
            STATE["assembling"] = False


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json", no_store=False):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if no_store:
            self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(data)

    # 视频按扩展名给正确的 Content-Type，不然 .webm/.mkv 的成片浏览器直接不播
    _MIME_VID = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
                 ".webm": "video/webm", ".mkv": "video/x-matroska", ".avi": "video/x-msvideo"}

    def _serve_video(self, path):
        """
        分块流式发送视频（1MB 一块）。
        以前是把整个 Range 段 f.read(length) 读进内存再写出去，
        bytes=0- 就等于把整部成片读进内存 —— 导出面板里一排 <video> 缩略图
        能把内存顶出个大尖峰。客户端断开就停。
        """
        size = os.path.getsize(path)
        mime = self._MIME_VID.get(os.path.splitext(path)[1].lower(), "application/octet-stream")
        start, end, partial = 0, size - 1, False
        rng = self.headers.get("Range") or ""
        if rng.startswith("bytes="):
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else size - 1
                else:                                  # bytes=-N：最后 N 字节
                    start = max(0, size - int(m.group(2)))
                end = min(end, size - 1)
                if start > end or start >= size:
                    start, end = 0, size - 1
                else:
                    partial = True
        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", mime)
        self.send_header("Accept-Ranges", "bytes")
        # 同一个段号重跑会覆盖成同名的 G01-x.mp4，URL 也就一样。
        # 不加这句，浏览器会拿缓存里的旧片直接播 —— 表现就是「点看片还是上一个视频」。
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(length))
        self.end_headers()
        chunk = 1 << 20
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    buf = f.read(min(chunk, left))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    left -= len(buf)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass            # 浏览器换片 / 关掉播放器，正常现象，不用往日志里刷

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path == "/api/video":
            path = ""
            nm = unquote(qs.get("name", [""])[0])
            if nm:
                path = find_out_file(nm)
            else:
                sid = unquote(qs.get("id", [""])[0])
                data = load_shots() or {"shots": []}
                for s in data["shots"]:
                    if s.get("id") == sid:
                        path = s.get("out") or ""
                        break
            if not path or not os.path.isfile(path):
                return self._send(404, {"error": "这一段还没生成好"})
            return self._serve_video(path)

        if u.path == "/api/poster":
            # 状态列那一小块视频封面：直接用 VHS 同时写出的 png，
            # 比让浏览器解码视频首帧快得多，也不会在无 GPU 环境里变黑框。
            sid = unquote(qs.get("id", [""])[0])
            out = ""
            for s in (load_shots() or {"shots": []})["shots"]:
                if s.get("id") == sid:
                    out = s.get("out") or ""
                    break
            png = os.path.splitext(out)[0] + ".png" if out else ""
            if not png or not os.path.isfile(png):
                return self._send(404, {"error": "还没封面"})
            with open(png, "rb") as f:
                return self._send(200, f.read(), "image/png", no_store=True)

        if u.path == "/api/asset":
            name = os.path.basename(unquote(qs.get("name", [""])[0]))
            p = os.path.join(CFG["assets"], name)
            if not os.path.isfile(p):
                return self._send(404, {"error": "not found"})
            ext = os.path.splitext(p)[1].lower()
            mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                    ".webp": "image/webp", ".bmp": "image/bmp"}.get(ext, "application/octet-stream")
            with open(p, "rb") as f:
                return self._send(200, f.read(), mime)

        if u.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "r", encoding="utf-8") as f:
                return self._send(200, f.read(), "text/html", no_store=True)
        if u.path == "/api/workflow":
            # 顺手同步 ComfyUI 里的节点名（你在 ComfyUI 里 Ctrl+S 保存过才触发）
            auto_msg = ""
            try:
                n = refresh_titles_from_comfy()
                if n:
                    auto_msg = f"已从 ComfyUI 同步 {n} 个节点名"
                else:
                    t2, p2, err2 = scan_comfy_titles(force=False)
                    if t2:
                        cur = dict(TITLES)
                        if any(cur.get(k) != v for k, v in t2.items()):
                            save_titles({**cur, **t2})
                            auto_msg = f"已从 ComfyUI 工作流文件同步 {len(t2)} 个节点名"
            except Exception:
                pass
            tpl = load_template() or {}
            det = wfmap.detect(tpl, TITLES) if tpl else {}
            nodes = []
            for nid, node in tpl.items():
                prv = ""
                for k, v in (node.get("inputs") or {}).items():
                    if isinstance(v, (str, int, float)) and not isinstance(v, bool):
                        prv = f"{k}={str(v)[:26]}"
                        break
                nodes.append({"id": nid, "class": (node.get("class_type") or ""),
                              "preview": prv, "title": node_title(nid, node),
                              "kind": "image" if (node.get("class_type") or "") in wfmap.IMG_CLASSES
                                      else ("video" if (node.get("class_type") or "") in wfmap.VIDEO_CLASSES else "")})
            nodes.sort(key=lambda x: (0, int(x["id"])) if x["id"].isdigit() else (1, 0))
            tpls = [f for f in sorted(os.listdir(HERE))
                    if f.lower().endswith(".json")
                    and f not in ("config.json", "workflow_map.json", "shots.json", "node_titles.json")]
            return self._send(200, {
                "template": CFG["template"], "templates": tpls,
                "hash": wfmap.template_hash(tpl) if tpl else "",
                "mapping": get_mapping() or {}, "detected": det,
                "all_images": det.get("all_images", []),
                "all_videos": det.get("all_videos", []),
                "nodes": nodes, "summary": wfmap.summarize(get_mapping() or {}),
                "fps": CFG.get("fps") or 24,
                "auto_titles": auto_msg,
                "wf_dir": comfy_workflow_dir()
            })

        if u.path == "/api/state":
            data = load_shots() or {"shots": []}
            prog = dict(STATE.get("progress") or {})
            if prog.get("t0"):
                prog["elapsed"] = int(time.time() - prog["t0"])
            mm = get_mapping() or {}
            n_img = len([e for e in (mm.get("images") or []) if (e or {}).get("node")])
            n_vid = len([e for e in (mm.get("videos") or []) if (e or {}).get("node")])
            return self._send(200, {
                "shots": data.get("shots", []),
                "meta": {"doc": CFG["doc"], "assets": CFG["assets"], "onepass": CFG.get("onepass"),
                         "comfy": CFG.get("comfy") or "",
                         "comfy_dir": (os.path.dirname(os.path.normpath(CFG.get("comfy_output_dir") or ""))
                                       if (CFG.get("comfy_output_dir") or "").strip() else ""),
                         "comfy_output_dir": CFG.get("comfy_output_dir") or "",
                         "extra_texts": mm.get("extra_texts") or [],
                         "auto_retry": CFG.get("auto_retry") or 0,
                         "auto_hide_days": CFG.get("auto_hide_days") or 0,
                         "snippets": CFG.get("snippets") or [],
                         "engine": cur_engine()},
                "assets": list_assets(),
                "videos": list_videos(),
                "img_slots": n_img,
                "vid_slots": n_vid,
                "running": STATE["running"], "current": STATE["current"],
                "assembling": bool(STATE.get("assembling")),
                "asm_prog": dict(STATE.get("asm_prog") or {}),
                "film": STATE.get("last_film") or "",
                "progress": prog,
                "queue": STATE.get("queue_remaining", 0),
                "export": dict(STATE.get("export") or {}),
                "ws": bool(WS and WS.connected),
                "log": STATE["log"][-200:], "error": STATE["last_error"],
                "comfy": comfy_online(2.5)     # 2.5 秒内复用上一次的探测结果
            })
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        n = int(self.headers.get("Content-Length") or 0)

        # 文件上传（原始二进制，name/target 走 query）
        if u.path == "/api/upload":
            name = unquote(qs.get("name", ["file.bin"])[0])
            target = qs.get("target", ["assets"])[0]
            if target == "doc":
                d = os.path.join(HERE, "_docs")
            elif target == "workflow":
                d = HERE
            else:
                # 上传素材就传到「当前素材库」目录，符合直觉
                d = CFG.get("assets") if os.path.isdir(CFG.get("assets") or "") else os.path.join(HERE, "_assets")
            os.makedirs(d, exist_ok=True)
            safe = re.sub(r'[\\/:*?"<>|]', "_", name)
            p = os.path.join(d, safe)
            body = self.rfile.read(n) if n else b""
            with open(p, "wb") as f:
                f.write(body)
            if target == "doc":
                CFG["doc"] = p
            elif target == "workflow":
                obj = load_json(p)
                if not obj:
                    try:
                        os.remove(p)
                    except Exception:
                        pass
                    return self._send(400, {"error": "这个 json 读不出来，请确认是 ComfyUI 导出的工作流"})
                if isinstance(obj.get("nodes"), list):
                    # ComfyUI 界面「保存」的工作流：只用来导入节点名字（图1/图2/种子…）
                    titles = {}
                    for nd in obj["nodes"]:
                        nm = (nd.get("title") or "").strip()
                        if nm and nd.get("id") is not None:
                            titles[str(nd["id"])] = nm
                    save_titles({**TITLES, **titles})
                    try:
                        os.remove(p)
                    except Exception:
                        pass
                    try:
                        ensure_mapping(force=True)
                    except Exception:
                        pass
                    log(f"已从界面工作流导入 {len(titles)} 个节点名")
                    return self._send(200, {"ok": True, "kind": "ui", "titles": len(titles),
                                            "msg": f"已导入 {len(titles)} 个节点名，映射已重新识别"})
                CFG["template"] = safe
                if not TITLES:
                    seed_titles_from_template(obj)
            save_json(CONFIG_PATH, CFG)
            return self._send(200, {"ok": True, "saved": p, "size": len(body)})

        body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

        if u.path == "/api/config":
            if CONFIG_BROKEN:
                return self._send(400, {"error": "config.json 现在是坏的，为了不覆盖你的原文件，"
                                                 "这次没有保存。\n先修好 config.json（或从 _bak/config.json.bad-* 找回），"
                                                 "再重启导演台。"})
            _old_eng = cur_engine()
            for k in ("doc", "assets", "onepass", "comfy_input_subdir", "project_name", "fps",
                      "comfy_output_dir", "comfy_dir", "comfy",
                      "auto_retry", "auto_hide_days", "snippets",
                      "engine", "budget_usd"):
                if k in body:
                    CFG[k] = body[k]
            if cur_engine() != _old_eng:
                log("生成引擎已切换 → " + engines.engine_label(cur_engine()))
            # 选了 ComfyUI 安装目录 → 顺手把输出目录指到它下面的 output（仅当前输出目录无效时才改）
            cd = (CFG.get("comfy_dir") or "").strip().strip('"')
            if "comfy_dir" in body and cd and os.path.isdir(cd):
                od = os.path.join(cd, "output")
                if os.path.isdir(od) and not os.path.isdir(CFG.get("comfy_output_dir") or ""):
                    CFG["comfy_output_dir"] = od
                    log("已按 ComfyUI 目录自动设置输出目录：" + od)
            save_json(CONFIG_PATH, CFG)
            return self._send(200, {"ok": True, "config": CFG})

        # ---- 工作流映射 ----
        if u.path == "/api/workflow/save":
            m = body.get("mapping") or {}
            tpl = load_template() or {}
            m["template_hash"] = wfmap.template_hash(tpl) if tpl else ""
            m["fps"] = body.get("fps") or CFG.get("fps") or 24
            CFG["fps"] = m["fps"]
            save_json(CONFIG_PATH, CFG)
            globals()["MAP"] = wfmap.clean(m)
            save_json(MAP_PATH, MAP)
            log("工作流映射已保存：" + wfmap.summarize(MAP))
            return self._send(200, {"ok": True, "summary": wfmap.summarize(MAP)})

        if u.path == "/api/workflow/detect":
            try:
                m = ensure_mapping(force=True)
                det = wfmap.detect(load_template() or {}, TITLES)
                det["mapping"] = m
                return self._send(200, {"ok": True, "mapping": m, "detected": det,
                                        "summary": wfmap.summarize(m)})
            except Exception as e:
                return self._send(500, {"error": str(e)})

        if u.path == "/api/import_comfy":
            try:
                g, err = import_from_comfy()
            except Exception as e:
                return self._send(500, {"error": "抓取失败：" + str(e)})
            if not g:
                return self._send(200, {"ok": False, "error": err})
            try:
                m = ensure_mapping(force=True)
            except Exception as e:
                return self._send(500, {"error": str(e)})
            nimg = len([e for e in (m.get("images") or []) if (e or {}).get("node")])
            nvid = len([e for e in (m.get("videos") or []) if (e or {}).get("node")])
            log(f"从 ComfyUI 导入工作流：{len(g)} 节点 · 图槽 {nimg} · 视频槽 {nvid}")
            return self._send(200, {"ok": True, "nodes": len(g), "images": nimg,
                                    "videos": nvid, "summary": wfmap.summarize(m)})

        if u.path == "/api/rename_title":
            nid = str(body.get("id") or "").strip()
            title = (body.get("title") or "").strip()
            if not nid:
                return self._send(400, {"error": "缺少节点 id"})
            t = dict(TITLES)
            if title:
                t[nid] = title
                LOCKS.add(nid)
            else:
                t.pop(nid, None)
                LOCKS.discard(nid)
            save_titles(t)
            save_locks()
            log(f"节点名：#{nid} → {title or '（恢复默认）'}（已锁定，不会被自动同步覆盖）")
            return self._send(200, {"ok": True})

        if u.path == "/api/params":
            tpl = load_template() or {}
            out = []
            for nid, n in tpl.items():
                if not isinstance(n, dict):
                    continue
                ct = (n.get("class_type") or "")
                for k, v in (n.get("inputs") or {}).items():
                    if isinstance(v, (list, bool)):
                        continue
                    if not isinstance(v, (int, float, str)):
                        continue
                    if k.lower() in ("seed", "noise_seed"):
                        continue
                    if isinstance(v, str) and len(v) > 60:
                        continue          # 长文本（提示词之类）不列
                    out.append({
                        "node": nid, "input": k, "value": v, "class": ct,
                        "title": node_title(nid, n) or ct,
                        "numeric": isinstance(v, (int, float)),
                        "options": combo_options(ct, k),
                    })
            out.sort(key=lambda x: (0 if x["numeric"] else 1, str(x["title"]), _idkey_str(x["node"])))
            # 文本类节点单独列出来，供「附加文本槽」挑选（不受 60 字符长度限制）
            texts = []
            for nid, n in tpl.items():
                if not isinstance(n, dict):
                    continue
                ct = (n.get("class_type") or "")
                ins = n.get("inputs") or {}
                key = "value" if ct.startswith("PrimitiveString") else ("text" if ct in wfmap.TEXT_CLASSES else None)
                if not key or not isinstance(ins.get(key), str):
                    continue
                texts.append({"node": nid, "input": key, "class": ct,
                              "title": node_title(nid, n) or ct,
                              "value": ins.get(key)})
            texts.sort(key=lambda x: _idkey_str(x["node"]))
            return self._send(200, {"params": out,
                                    "text_nodes": texts,
                                    "extra": (get_mapping() or {}).get("extra") or [],
                                    "extra_texts": (get_mapping() or {}).get("extra_texts") or []})

        if u.path == "/api/scan_titles":
            titles, path, err = scan_comfy_titles(force=True)
            if not titles:
                return self._send(200, {"ok": False, "error": err or "没扫到节点名"})
            changed = {k: v for k, v in titles.items() if TITLES.get(k) != v}
            save_titles({**TITLES, **titles})
            try:
                m = ensure_mapping(force=True)
            except Exception as e:
                return self._send(500, {"error": str(e)})
            if changed:
                log(f"节点名同步：{len(changed)} 个有变化（{os.path.basename(path)}）")
            else:
                log("节点名同步：文件读到了，但名字和当前一样（ComfyUI 里可能还没保存）")
            return self._send(200, {"ok": True, "count": len(titles), "changed": len(changed),
                                    "samples": [f"{k} → {v}" for k, v in list(changed.items())[:6]],
                                    "file": path, "summary": wfmap.summarize(m), "mapping": m})

        if u.path == "/api/workflow/template":
            name = body.get("name") or ""
            p = os.path.join(HERE, name)
            if not os.path.isfile(p):
                return self._send(404, {"error": "模板不存在：" + name})
            if not load_json(p):
                return self._send(400, {"error": "这个 json 读不出来（需要 ComfyUI 的 API 格式，不是界面另存的工作流）"})
            CFG["template"] = name
            save_json(CONFIG_PATH, CFG)
            try:
                m = ensure_mapping(force=True)
            except Exception as e:
                return self._send(500, {"error": str(e)})
            log(f"已切换工作流：{name} · " + wfmap.summarize(m))
            return self._send(200, {"ok": True, "mapping": m, "summary": wfmap.summarize(m)})

        if u.path == "/api/parse":
            try:
                shots = reparse()
                alive = len([s for s in shots if not s.get("deleted")])
                return self._send(200, {"ok": True, "count": alive,
                                        "deleted": len(shots) - alive})
            except Exception as e:
                return self._send(500, {"error": str(e)})

        if u.path == "/api/detect_paths":
            try:
                r = detect_paths(body.get("path") or "")
            except Exception as e:
                return self._send(500, {"error": "识别失败：" + str(e)})
            if r.get("error"):
                return self._send(400, {"error": r["error"]})
            return self._send(200, dict(r, ok=True))

        if u.path == "/api/choose":
            # 一次把「文档 + 素材文件夹」都定下来，然后重新解析（前端不用连着调三个接口）
            doc = (body.get("doc") or "").strip().strip('"')
            assets = (body.get("assets") or "").strip().strip('"')
            if doc and not os.path.isfile(doc):
                return self._send(400, {"error": "文档不存在：" + doc})
            if assets and not os.path.isdir(assets):
                return self._send(400, {"error": "素材文件夹不存在：" + assets})
            if doc:
                CFG["doc"] = doc
            if assets:
                CFG["assets"] = assets
            save_json(CONFIG_PATH, CFG)
            out = {"ok": True, "doc": CFG["doc"], "assets": CFG["assets"], "count": 0}
            if doc:
                try:
                    shots = reparse()
                    out["count"] = len([s for s in shots if not s.get("deleted")])
                except Exception as e:
                    out["parse_error"] = str(e)
            return self._send(200, out)

        if u.path == "/api/export_doc":
            dest = (body.get("dest") or "").strip().strip('"')
            if not dest:
                return self._send(400, {"error": "请先选择保存位置"})
            data = load_shots() or {"shots": []}
            shots = [x for x in (data.get("shots") or []) if not x.get("deleted")]
            if not shots:
                return self._send(400, {"error": "现在还没有分段，先选一份文档解析出来"})
            d = os.path.dirname(dest)
            if d and not os.path.isdir(d):
                return self._send(400, {"error": "这个文件夹不存在：" + d})
            enc = "utf-8-sig" if dest.lower().endswith(".txt") else "utf-8"
            try:
                with open(dest, "w", encoding=enc) as f:
                    f.write(build_doc_markdown(shots))
            except Exception as e:
                return self._send(500, {"error": "写文件失败：" + str(e)})
            log("已导出修改后的文档：%s（%d 段）" % (dest, len(shots)))
            return self._send(200, {"ok": True, "path": dest, "count": len(shots)})

        if u.path == "/api/backups":
            cur = load_shots() or {}
            return self._send(200, {
                "ok": True,
                "current": len([x for x in (cur.get("shots") or []) if not x.get("deleted")]),
                "items": list_backups(),
            })

        if u.path == "/api/restore_backup":
            name = os.path.basename((body.get("file") or "").strip())
            if not name.lower().endswith(".json"):
                return self._send(400, {"error": "备份文件名不对"})
            src = os.path.join(HERE, "_bak", name)
            if not os.path.isfile(src):
                return self._send(404, {"error": "这个备份不存在了：" + name})
            data = load_json(src) or {}
            if "shots" not in data:
                return self._send(400, {"error": "这个文件里没有分段数据"})
            # 先把现在的存一份，后悔了还能退回来
            stamp = ""
            try:
                stamp = time.strftime("%Y%m%d-%H%M%S")
                if os.path.isfile(SHOTS_PATH):
                    shutil.copy2(SHOTS_PATH, os.path.join(
                        HERE, "_bak", "shots.before_restore-%s.json" % stamp))
            except Exception:
                stamp = ""
            save_json(SHOTS_PATH, data)
            doc = (data.get("doc") or "").strip()
            if doc and os.path.isfile(doc):
                CFG["doc"] = doc
                save_json(CONFIG_PATH, CFG)
            elif doc:
                CFG["doc"] = doc          # 路径照旧写回，哪怕文件现在不在
                save_json(CONFIG_PATH, CFG)
            n = len(data.get("shots") or [])
            log("已从备份恢复：%s（%d 段）%s"
                % (name, n, ("，当前数据已存为 shots.before_restore-%s.json" % stamp) if stamp else ""))
            return self._send(200, {"ok": True, "count": n,
                                    "saved_as": ("shots.before_restore-%s.json" % stamp) if stamp else ""})

        if u.path == "/api/clear_doc":
            # 清空当前文档解析出来的所有分段；文档文件本身不动，只解除引用
            old = load_shots() or {}
            n = len(old.get("shots") or [])
            stamp = ""
            try:
                bak_dir = os.path.join(HERE, "_bak")
                os.makedirs(bak_dir, exist_ok=True)
                stamp = time.strftime("%Y%m%d-%H%M%S")
                if os.path.isfile(SHOTS_PATH):
                    shutil.copy2(SHOTS_PATH,
                                 os.path.join(bak_dir, "shots.cleared-%s.json" % stamp))
            except Exception:
                stamp = ""
            # 素材目录要保住：清空的是「文档和分段」，不是素材库。
            # 内存里的 CFG["assets"] 万一是空的（配置被别的地方覆盖过），
            # 就从旧的 shots.json 里把它捡回来，别跟着一起清没。
            _keep_assets = (CFG.get("assets") or "").strip() or (old.get("assets") or "").strip()
            save_json(SHOTS_PATH, {"doc": "", "assets": _keep_assets, "shots": []})
            if _keep_assets:
                CFG["assets"] = _keep_assets
            CFG["doc"] = ""
            save_json(CONFIG_PATH, CFG)
            # 同步清空素材库：图片移到 _trash 文件夹保留（不删除源文件）
            moved = 0
            try:
                _trash_dir = os.path.join(HERE, "_trash")
                os.makedirs(_trash_dir, exist_ok=True)
                for _f in list_assets(include_hidden=True):
                    _src = os.path.join(_keep_assets, _f) if _keep_assets else ""
                    if not _src or not os.path.isfile(_src):
                        continue
                    _st, _ex = os.path.splitext(_f)
                    _dst = os.path.join(_trash_dir, _f)
                    _k = 2
                    while os.path.exists(_dst):
                        _dst = os.path.join(_trash_dir, "%s(%d)%s" % (_st, _k, _ex))
                        _k += 1
                    shutil.move(_src, _dst)
                    moved += 1
            except Exception as e:
                log("清空素材库时出错：" + str(e)[:120])
            log("已清空文档，删除 %d 段分段%s%s"
                % (n, ("（备份 _bak/shots.cleared-%s.json）" % stamp) if stamp else "",
                   ("，素材 %d 张移入 _trash 保留（未删除）" % moved) if moved else ""))
            return self._send(200, {"ok": True, "count": n, "backup": stamp,
                                    "moved_assets": moved})

        if u.path == "/api/save":
            # 只合并「界面上能改的字段」，status / out / err / ver 这些是后台在写，
            # 不能被浏览器那份旧快照盖掉（否则生成中的状态会被打回待生成）
            # 整段读-改-写都在 WRLOCK 里：生成线程同一时刻也在写，不加锁会互相覆盖
            EDITABLE = ("title", "sec", "prompt", "prompt_cn", "lang", "extra_texts",
                        "skip", "seed", "pic_more", "no_pic", "note",
                        "edited", "edited_cn", "edited_pic", "pic_n", "vid_n", "vid_more")
            with WRLOCK:
                data = load_shots() or {"shots": []}
                cur = {x.get("id"): x for x in (data.get("shots") or [])}
                for inc in (body.get("shots") or []):
                    sid = inc.get("id")
                    tgt = cur.get(sid)
                    if not tgt:
                        continue
                    for k in EDITABLE:
                        if k in inc:
                            tgt[k] = inc[k]
                    for i in range(1, 10):
                        k = "pic%d" % i
                        if k in inc:
                            tgt[k] = inc[k]
                        k2 = "raw%d" % i
                        if k2 in inc:
                            tgt[k2] = inc[k2]
                    for i in range(1, 6):
                        k = "vid%d" % i
                        if k in inc:
                            tgt[k] = inc[k]
                save_json(SHOTS_PATH, data)
            return self._send(200, {"ok": True})

        # ---- 删段 / 恢复段（软删除，随时可还原）----
        if u.path == "/api/hidden":
            h = load_hidden()
            return self._send(200, {"ok": True,
                                    "assets": len(h.get("assets") or {}),
                                    "clips": len(h.get("clips") or {}),
                                    "auto_retry": CFG.get("auto_retry") or 0,
                                    "auto_hide_days": CFG.get("auto_hide_days") or 0,
                                    "snippets": CFG.get("snippets") or []})

        if u.path == "/api/auto_hide":
            try:
                days = int(body.get("days") or 0)
            except Exception:
                days = 0
            if days <= 0:
                return self._send(400, {"error": "天数要大于 0"})
            days = min(30, days)
            targets = body.get("targets") or ["assets", "clips"]
            cutoff = time.time() - days * 86400
            hid = load_hidden()
            n_a = n_c = 0
            if "assets" in targets:
                d = CFG.get("assets") or ""
                if os.path.isdir(d):
                    for f in os.listdir(d):
                        if not f.lower().endswith(IMG_EXT):
                            continue
                        pth = os.path.join(d, f)
                        try:
                            if os.path.getmtime(pth) < cutoff:
                                hid["assets"][f] = int(time.time())
                                n_a += 1
                        except Exception:
                            pass
            if "clips" in targets:
                d = out_root()
                used = set()
                for x in ((load_shots() or {}).get("shots") or []):
                    if x.get("out"):
                        used.add(os.path.basename(x["out"]))
                if os.path.isdir(d):
                    for f in os.listdir(d):
                        if not f.lower().endswith((".mp4", ".mov", ".webm", ".mkv")):
                            continue
                        if f in used:
                            continue          # 还在被某一段用的，不动
                        pth = os.path.join(d, f)
                        try:
                            if os.path.getmtime(pth) < cutoff:
                                hid["clips"][f] = int(time.time())
                                n_c += 1
                        except Exception:
                            pass
            save_hidden(hid)
            log("按「%d 天没动过」隐藏：素材 %d 个、成片 %d 个（文件仍在原处，没删）"
                % (days, n_a, n_c))
            return self._send(200, {"ok": True, "assets": n_a, "clips": n_c})

        if u.path == "/api/unhide_all":
            # 只清「隐藏」标记（老版本回收站的 trash_* 残留标记也一并清掉）
            h = load_hidden()
            n_hidden = len(h.get("assets") or {}) + len(h.get("clips") or {})
            save_hidden({"assets": {}, "clips": {}, "trash_assets": {}, "trash_videos": {}})
            # 「已删除」列表里被移除的段也一起放回来（数据一直都在）
            back = {"n": 0}

            def _unpurge(d):
                for s in d.get("shots", []):
                    if s.get("purged"):
                        s.pop("purged", None)
                        back["n"] += 1
                return True

            try:
                mutate_shots(_unpurge)
            except Exception:
                pass
            log("已恢复显示：隐藏的素材/成片 %d 个、从「已删除」列表移除的段 %d 个"
                % (n_hidden, back["n"]))
            return self._send(200, {"ok": True, "hidden": n_hidden,
                                    "shots": back["n"]})

        if u.path == "/api/logs":
            which = (body.get("which") or qs.get("which", ["cur"])[0] or "cur")
            path = LOG_PATH if which != "prev" else LOG_PATH + ".1"
            if not os.path.isfile(path):
                return self._send(404, {"error": "没有这个日志文件"})
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                txt = f.read()
            return self._send(200, {"ok": True, "which": which,
                                    "size": len(txt), "text": txt})

        if u.path == "/api/add_shot":
            sid = (body.get("id") or "").strip()
            if not sid:
                return self._send(400, {"error": "段号不能为空"})
            if not re.match(r"^[A-Za-z]*\d+$", sid):
                return self._send(400, {"error": "段号只能是「字母 + 数字」，比如 G23、A01"})
            try:
                sec = int(body.get("sec") or 8)
            except Exception:
                sec = 8
            sec = max(1, min(60, sec))
            box = {}

            def _add(d):
                shots = d.get("shots") or []
                # 已删除的段不该占着段号：同名且已删除的直接顶掉，只有「还在用」的才报已存在
                hit = -1
                for _i, _x in enumerate(shots):
                    if (_x.get("id") or "") == sid:
                        if _x.get("deleted") or _x.get("purged"):
                            hit = _i
                            break
                        box["err"] = "段号已存在：" + sid
                        return False
                # 时间码接着上一段的结束时间往后排
                start, last = 0, ""
                for x in shots:
                    if not x.get("deleted") and x.get("tc"):
                        last = x.get("tc")
                m = re.search(r"-\s*(\d{1,2}):(\d{2})", last or "")
                if m:
                    start = int(m.group(1)) * 60 + int(m.group(2))
                tc = "%02d:%02d-%02d:%02d" % (start // 60, start % 60,
                                              (start + sec) // 60, (start + sec) % 60)
                shot = {
                    "id": sid,
                    "title": (body.get("title") or "").strip() or "新增第 %d 段" % (len(shots) + 1),
                    "tc": tc,
                    "sec": sec,
                    "prompt": body.get("prompt") or "",
                    "prompt_cn": body.get("prompt_cn") or "",
                    "lang": "cn" if (body.get("lang") or "en") == "cn" else "en",
                    "extra_texts": {},
                    "skip": False,
                    "status": "pending",
                    "out": "",
                    "seed": 0,
                    "pic_n": 3,
                    "vid_n": 0,
                    "pic_more": 0,
                    "ver": 0,
                    "no_pic": bool(body.get("no_pic")),
                    "note": "手动新增的段（不用参考图）" if body.get("no_pic") else "手动新增的段",
                }
                for k in range(1, 10):
                    shot["pic%d" % k] = ""
                    shot["raw%d" % k] = ""
                    shot["score%d" % k] = 0.0
                for k in range(1, 6):
                    shot["vid%d" % k] = ""
                    shot["rawvid%d" % k] = ""
                if hit >= 0:
                    shots[hit] = shot        # 顶掉那个已删除的同名段
                else:
                    shots.append(shot)
                d["shots"] = shots
                box["shot"] = shot
                box["count"] = len(shots)
                return True

            mutate_shots(_add)
            if box.get("err"):
                return self._send(400, {"error": box["err"]})
            shot = box.get("shot") or {}
            log("新增一段：%s「%s」%d 秒（%s）" % (sid, shot.get("title", ""), sec, shot.get("tc", "")))
            return self._send(200, {"ok": True, "shot": shot, "count": box.get("count", 0)})

        if u.path == "/api/rename_shot":
            old = (body.get("old") or "").strip()
            new = (body.get("new") or "").strip()
            if not old or not new:
                return self._send(400, {"error": "缺少原段号或新段号"})
            if not re.match(r"^[A-Za-z]*\d+$", new):
                return self._send(400, {"error": "段号只能是「字母 + 数字」，比如 G23、A01"})
            box = {}

            def _ren(d):
                shots = d.get("shots") or []
                if any((x.get("id") or "") == new for x in shots):
                    box["err"] = "段号已存在：" + new
                    return False
                hit = None
                for x in shots:
                    if (x.get("id") or "") == old:
                        hit = x
                        break
                if not hit:
                    box["err"] = "找不到这一段：" + old
                    box["code"] = 404
                    return False
                if hit.get("out"):
                    box["err"] = ("这段已经生成过视频，改段号会让文件名对不上。\n"
                                  "先删掉它的成片（导出面板里删除），或者删掉这段重新加。")
                    return False
                hit["id"] = new
                return True

            mutate_shots(_ren)
            if box.get("err"):
                return self._send(box.get("code") or 400, {"error": box["err"]})
            log("段号已改：%s → %s" % (old, new))
            return self._send(200, {"ok": True})

        if u.path == "/api/delete_shot":
            tid = body.get("id", "")
            box = {}

            # 删之前先留一份快照：真删了之后只有 _bak 能找回
            try:
                _bd = os.path.join(HERE, "_bak")
                os.makedirs(_bd, exist_ok=True)
                if os.path.isfile(SHOTS_PATH):
                    shutil.copy2(SHOTS_PATH, os.path.join(
                        _bd, "shots.before-del-%s-%s.json" % (tid, time.strftime("%H%M%S"))))
            except Exception:
                pass

            def _del(d):
                shots = d.get("shots") or []
                keep = [x for x in shots if x.get("id") != tid]
                box["hit"] = len(shots) - len(keep)
                d["shots"] = keep          # 直接从数据里移除，不再留 deleted 标记
                return True

            mutate_shots(_del)
            log(f"已删除段 {tid}" if box.get("hit") else f"删除失败：找不到 {tid}")
            return self._send(200, {"ok": bool(box.get("hit"))})

        if u.path == "/api/restore_shot":
            tid = body.get("id", "")

            def _res(d):
                for s in d.get("shots", []):
                    if s.get("id") == tid:
                        s["deleted"] = False
                return True

            mutate_shots(_res)
            return self._send(200, {"ok": True})

        if u.path == "/api/restore_shots":

            def _all(d):
                for s in d.get("shots", []):
                    s["deleted"] = False
                return True

            mutate_shots(_all)
            log("已恢复全部被删段")
            return self._send(200, {"ok": True})

        # ---- 批量处理 ----
        if u.path == "/api/purge_batch":
            kind = (body.get("kind") or "").strip()
            keys = body.get("keys") or []
            if not isinstance(keys, list) or not keys:
                return self._send(400, {"error": "没有选中任何东西"})
            done, failed = [], []
            if kind == "shots":
                want = set(str(k) for k in keys)
                hit = {"n": 0}

                def _purge(d):
                    for s in d.get("shots", []):
                        if str(s.get("id")) in want and s.get("deleted"):
                            s["purged"] = True    # 只是不再出现在「已删除」列表里，数据还在
                            hit["n"] += 1
                    return True

                mutate_shots(_purge)
                done = sorted(want)
                log(f"从「已删除」列表里移除 {hit['n']} 段（数据仍保留在 shots.json，没有真删）")
            else:
                return self._send(400, {"error": "不认识的类型：" + kind})
            return self._send(200, {"ok": True, "done": done, "failed": failed})

        # ---- 删素材：只从素材库里移除显示，文件保留在原位置（不允许删除源文件）----
        if u.path == "/api/delete_asset":
            name = os.path.basename(body.get("name", ""))
            if not name:
                return self._send(400, {"error": "没给文件名"})
            src = os.path.join(CFG["assets"], name)
            already = name in hidden_set("assets")
            # 已在隐藏名单里 = 刚才那一下已经删成功（双击 / 浏览器重发），别报错
            h = load_hidden()
            h.setdefault("assets", {})[name] = int(time.time())
            save_hidden(h)
            affected = []

            def _clear(d):
                for s in d.get("shots", []):
                    for slot in range(1, 10):
                        k = "pic%d" % slot
                        if s.get(k) == name:
                            s[k] = ""
                            affected.append(s["id"])
                return True

            mutate_shots(_clear)
            log(f"素材已从素材库移除（文件保留在原位置）：{name}"
                + (f"（{len(set(affected))} 段引用已清空）" if affected else ""))
            return self._send(200, {"ok": True,
                                    "affected": sorted(set(affected)),
                                    "already": already or not os.path.isfile(src)})

        if u.path == "/api/rename_asset":
            old = os.path.basename(body.get("old", ""))
            new = os.path.basename(body.get("new", "")).strip()
            if not old or not new:
                return self._send(400, {"error": "名字不能为空"})
            if not os.path.splitext(new)[1]:
                new += os.path.splitext(old)[1]
            new = re.sub(r'[\\/:*?"<>|]', "_", new)
            src = os.path.join(CFG["assets"], old)
            dst = os.path.join(CFG["assets"], new)
            if not os.path.isfile(src):
                # 老名字已经不在了，但新名字在 —— 说明上一次其实改成功了（比如连点两下、
                # 或者浏览器重发了这个请求）。这种情况要当成功处理，不能报「素材不存在」，
                # 否则用户看到报错、重进却发现改名生效了，正是这个错乱。
                if os.path.isfile(dst):
                    return self._send(200, {"ok": True, "affected": [], "already": True})
                return self._send(404, {"error": "素材不存在：" + old})
            if old == new:
                return self._send(200, {"ok": True, "affected": []})
            if os.path.exists(dst):
                return self._send(409, {"error": "素材库里已经有叫「" + new + "」的文件了"})
            os.rename(src, dst)
            # 改名也算一次「修改」：刷新文件时间，让它排到素材库最前面
            # （素材库是按修改时间倒序排的）
            try:
                os.utime(dst, None)
            except Exception:
                pass
            affected = []

            def _ren(d):
                for s in d.get("shots", []):
                    for slot in range(1, 10):
                        k = "pic%d" % slot
                        if s.get(k) == old:
                            s[k] = new
                            affected.append(s["id"])
                return True

            mutate_shots(_ren)
            log(f"素材改名：{old} → {new}"
                + (f"（同步 {len(set(affected))} 段引用）" if affected else ""))
            return self._send(200, {"ok": True, "affected": sorted(set(affected))})

        if u.path == "/api/rematch":
            assets = list_assets(include_hidden=True)
            changed = []

            def _rem(d):
                for s in d.get("shots", []):
                    # 图槽 1–9 全都要重新匹配（原来只处理 1–3）
                    for slot in range(1, 10):
                        raw = (s.get("raw%d" % slot) or "").strip()
                        if not raw:
                            continue
                        f, sc = match_asset(raw, assets)
                        if f and s.get("pic%d" % slot) != f:
                            s["pic%d" % slot] = f
                            changed.append(s["id"])
                return True

            mutate_shots(_rem)
            return self._send(200, {"ok": True, "changed": sorted(set(changed))})

        if u.path == "/api/run":
            data = load_shots() or {"shots": []}
            shots = data["shots"]
            if body.get("all"):
                idx = [i for i, s in enumerate(shots) if not s.get("skip") and not s.get("deleted")]
            elif body.get("missing"):
                idx = [i for i, s in enumerate(shots) if not s.get("skip")
                       and not s.get("deleted") and s.get("status") != "done"]
            elif "index" in body:
                idx = [int(body["index"])]
            elif "ids" in body:
                want = set(body["ids"])
                idx = [i for i, s in enumerate(shots)
                       if s["id"] in want and not s.get("skip") and not s.get("deleted")]
            else:
                idx = []
            if STATE["running"]:
                return self._send(409, {"error": "正在运行中"})
            bulk = bool(body.get("all") or body.get("missing") or "ids" in body)
            threading.Thread(target=run_indices, args=(idx, bulk), daemon=True).start()
            return self._send(200, {"ok": True, "count": len(idx)})

        if u.path == "/api/stop":
            STOP.set()
            log("收到停止指令，正在打断 ComfyUI 当前任务…")
            threading.Thread(target=_interrupt_comfy, daemon=True).start()
            return self._send(200, {"ok": True})

        if u.path == "/api/assemble":
            if STATE["running"]:
                return self._send(409, {"error": "正在生成，先等完成（或点「停止」）"})
            dest = (body.get("dest") or "").strip().strip('"')
            if dest and not os.path.isdir(dest):
                return self._send(400, {"error": "这个文件夹不存在：" + dest
                                                 + "\n（界面上用「选择文件夹」挑一个已有的目录）"})
            with LOCK:
                if STATE.get("assembling"):
                    return self._send(409, {"error": "上一次合成还没结束，等它跑完"})
                STATE["assembling"] = True
            ids = body.get("ids")
            if ids is not None and not len(ids):
                with LOCK:
                    STATE["assembling"] = False
                return self._send(400, {"error": "没有选择任何段落"})
            threading.Thread(target=assemble_job, args=(ids, dest or None),
                             daemon=True).start()
            log("开始合成" + ("：全部段落" if ids is None else "：自选 %d 段" % len(ids))
                + (("，存到 " + dest) if dest else ""))
            return self._send(200, {"ok": True, "count": (len(ids) if ids else None),
                                    "dest": dest or ""})

        if u.path in ("/api/pick_file", "/api/pick_folder", "/api/pick_save"):
            mode = "save" if u.path == "/api/pick_save" else ("file" if u.path == "/api/pick_file" else "dir")
            title = (body.get("title") or "").strip() or (
                "另存为" if mode == "save" else ("选择文件" if mode == "file" else "选择文件夹"))
            try:
                path = pick_dialog(
                    mode=mode, title=title,
                    initial=(body.get("initial") or "").strip().strip('"'),
                    kinds=(body.get("kinds") or "").strip(),
                    defname=(body.get("filename") or "").strip(),
                    ext=(body.get("ext") or "mp4").strip())
                return self._send(200, {"ok": True, "path": path})
            except Exception as e:
                log("选择失败：" + str(e)[:160])
                return self._send(500, {"error": "打开选择框失败：" + str(e)})

        # ---- 删成片：只从导出列表里移除，文件保留在原位置（不允许删除源文件）----
        if u.path == "/api/vtrash":
            fname = (body.get("file") or "").strip()
            sid = (body.get("id") or "").strip()
            with WRLOCK:
                data = load_shots() or {"shots": []}
                if fname:
                    base = os.path.basename(fname)
                    owner = None
                    for s in data["shots"]:
                        if os.path.basename(s.get("out") or "") == base:
                            owner = s
                            break
                    already = base in hidden_set("clips")
                    h = load_hidden()
                    h.setdefault("clips", {})[base] = int(time.time())
                    save_hidden(h)
                    if owner:
                        owner["out"] = ""
                        owner["status"] = "pending"
                        owner["err"] = ""
                    save_json(SHOTS_PATH, data)
                    log("成片已从导出列表移除（文件保留在原位置）：%s" % base)
                    return self._send(200, {"ok": True, "already": already})
                hit = None
                for s in data["shots"]:
                    if s.get("id") == sid:
                        hit = s
                        break
                if not hit:
                    return self._send(404, {"error": "找不到这一段：" + sid})
                p = (hit.get("out") or "").strip()
                base = os.path.basename(p) if p else ""
                if base:
                    h = load_hidden()
                    h.setdefault("clips", {})[base] = int(time.time())
                    save_hidden(h)
                hit["out"] = ""
                hit["status"] = "pending"
                hit["err"] = ""
                save_json(SHOTS_PATH, data)
                log(f"{sid} 的成片已从导出列表移除（文件保留在原位置：{base}）" if base
                    else f"{sid} 本来就没有成片")
                return self._send(200, {"ok": True})

        if u.path == "/api/saveas":
            fname = (body.get("file") or "").strip()
            sid = (body.get("id") or "").strip()
            dest = (body.get("dest") or "").strip().strip('"')
            if not dest:
                return self._send(400, {"error": "缺少目标路径"})
            src = ""
            if fname:
                src = find_out_file(fname)
            elif sid:
                data = load_shots() or {"shots": []}
                for s in data["shots"]:
                    if s.get("id") == sid:
                        src = s.get("out") or ""
                        break
            if not src or not os.path.isfile(src):
                return self._send(400, {"error": "这个视频还没生成好"})
            d = os.path.dirname(dest)
            if d:
                os.makedirs(d, exist_ok=True)
            shutil.copy2(src, dest)
            log("另存为：" + dest)
            return self._send(200, {"ok": True, "path": dest})

        if u.path == "/api/clips":
            d = out_root()
            # 迁移旧命名 / 补版本标记 / 修 out 指针：都是「修一次就够」的活，
            # 以前每次打开导出面板都全量跑一遍（还要写一次 shots.json），
            # 现在 5 分钟内只跑一次；确实想立刻重跑就传 {"maint": true}
            force_maint = bool(body.get("maint"))
            now = time.time()
            if force_maint or now - _CLIPS_MAINT["t"] > 300:
                _CLIPS_MAINT["t"] = now
                try:
                    migrate_old_clips(d)
                    if os.path.isdir(d):
                        sids = set()
                        for fn in os.listdir(d):
                            m = re.match(r"^([A-Za-z]*\d+)-\d+", fn)
                            if m:
                                sids.add(m.group(1))
                        for sid in sids:
                            fix_new_marks(d, sid)
                        sync_outs(d)
                except Exception:
                    pass
            data = load_shots() or {"shots": []}
            titles = {}
            for s in data.get("shots", []):
                titles[s.get("id", "")] = s.get("title", "")
            items = []
            _HID_CLIPS = hidden_set("clips")
            if os.path.isdir(d):
                for fn in os.listdir(d):
                    if not fn.lower().endswith((".mp4", ".mov", ".webm", ".mkv")):
                        continue
                    if fn in _HID_CLIPS:
                        continue
                    if re.search(r"_\d{5}", fn):
                        continue        # ComfyUI 原始后缀的残留（重命名前的），不列
                    p = os.path.join(d, fn)
                    if not os.path.isfile(p):
                        continue
                    m = re.match(r"^([A-Za-z]*\d+)-(\d+)", fn)
                    sid = m.group(1) if m else ""
                    ver = int(m.group(2)) if m else 0
                    items.append({
                        "file": fn, "name": _strip_new(fn), "id": sid, "ver": ver,
                        "is_new": NEW_MARK in fn, "title": titles.get(sid, ""),
                        "size": os.path.getsize(p), "mtime": int(os.path.getmtime(p)),
                        "duration": clip_duration(p),
                    })
            items.sort(key=lambda x: (0 if x["id"] else 1, x["id"], x["ver"], x["name"]))
            return self._send(200, {"items": items, "count": len(items)})

        if u.path == "/api/residue_list":
            items = list_residue()
            return self._send(200, {"items": items, "count": len(items),
                                    "size": sum(x["size"] for x in items)})

        if u.path == "/api/residue_purge":
            want = body.get("files") or None
            items = list_residue()
            if want is not None:
                keep = {os.path.basename(str(x)) for x in want}
                items = [x for x in items if x["file"] in keep]
            moved, failed, freed = 0, 0, 0
            _trash_dir = os.path.join(HERE, "_trash")
            os.makedirs(_trash_dir, exist_ok=True)
            for it in items:
                p = find_out_file(it["file"])
                if not os.path.isfile(p):
                    failed += 1
                    continue
                try:
                    # 移到 _trash 保留，不删除源文件
                    _st, _ex = os.path.splitext(it["file"])
                    _dst = os.path.join(_trash_dir, it["file"])
                    _k = 2
                    while os.path.exists(_dst):
                        _dst = os.path.join(_trash_dir, "%s(%d)%s" % (_st, _k, _ex))
                        _k += 1
                    shutil.move(p, _dst)
                    moved += 1
                    freed += it["size"]
                except Exception as e:
                    failed += 1
                    log("清理残留失败：%s（%s）" % (it["file"], e))
            log("清理未纳管文件：%d 个移入 _trash 保留（未删除），释放 %.1f MB%s"
                % (moved, freed / 1048576.0, ("，%d 个失败" % failed) if failed else ""))
            return self._send(200, {"ok": True, "moved": moved, "failed": failed,
                                    "freed": freed})

        if u.path == "/api/export":
            files = body.get("files") or []
            dest = (body.get("dest") or "").strip().strip('"')
            if not dest:
                return self._send(400, {"error": "请先选择保存文件夹"})
            if not os.path.isdir(dest):
                return self._send(400, {"error": "这个文件夹不存在：" + dest})
            if (STATE.get("export") or {}).get("running"):
                return self._send(409, {"error": "上一次导出还没结束，稍等一下"})
            if not files:
                return self._send(400, {"error": "没有勾选任何文件"})
            threading.Thread(target=do_export, args=(files, dest), daemon=True).start()
            return self._send(200, {"ok": True, "started": True, "total": len(files)})

        if u.path == "/api/openfolder":
            d = out_root()
            os.makedirs(d, exist_ok=True)
            try:
                os.startfile(d)
            except Exception:
                pass
            return self._send(200, {"ok": True})

        return self._send(404, {"error": "not found"})

    def log_message(self, *a):
        pass


def selfcheck():
    """启动自检：换电脑后最容易漏的几项，直接写进界面日志。"""
    ok, bad = [], []
    try:
        subprocess.run([_bin("ffmpeg"), "-version"], capture_output=True, timeout=15)
        ok.append("ffmpeg")
    except Exception:
        bad.append("没找到 ffmpeg —— 把 ffmpeg.exe 放到本工具目录，或者装好并加进系统 PATH")
    od = CFG.get("comfy_output_dir") or ""
    if od and os.path.isdir(od):
        ok.append("输出目录")
    else:
        bad.append("输出目录不存在：" + (od or "（还没设置）") + " → 点顶部「输出目录」旁的选择按钮")
    # ComfyUI 根目录不再单独配置，直接从输出目录的上一级推算（<根>\output）
    _od = (CFG.get("comfy_output_dir") or "").strip()
    cd = os.path.dirname(os.path.normpath(_od)) if _od else ""
    _eng = cur_engine()
    if cd and os.path.isdir(os.path.join(cd, "models")):
        ok.append("模型")
        need = [("diffusion_models", "DasiwaMinimaxH3"),
                ("text_encoders", "qwen3vl_32b_minimax_h3"),
                ("vae", "minimax_h3_video_vae"),
                ("vae", "minimax_h3_audio_vae"),
                ("loras", "minimax_h3_fl2v_turbo")]
        miss = []
        for sub, kw in need:
            d = os.path.join(cd, "models", sub)
            try:
                files = os.listdir(d) if os.path.isdir(d) else []
            except Exception:
                files = []
            if not any(kw.lower() in f.lower() for f in files):
                miss.append(kw)
        if miss:
            bad.append("ComfyUI 的 models 里没找到：" + "、".join(miss) + " → 生成会报找不到模型")
        # 模板里写死的模型名，逐个对一下（换过模型的电脑最容易踩这个）
        try:
            tpl = load_json(os.path.join(HERE, CFG["template"])) or {}
            gone = []
            for _nid, _n in tpl.items():
                if not isinstance(_n, dict):
                    continue
                hit = MODEL_SLOTS.get(_n.get("class_type") or "")
                if not hit:
                    continue
                key, dirs = hit
                name = (_n.get("inputs") or {}).get(key)
                if not isinstance(name, str) or not name.strip():
                    continue
                if not any(os.path.exists(os.path.join(cd, "models", d, name)) for d in dirs):
                    gone.append(name)
            if gone:
                bad.append("模板引用的模型在 ComfyUI 里找不到：" + "、".join(gone[:4])
                           + " → 换过模型就重新导出 API 工作流，在「工作流映射」里传上来")
        except Exception:
            pass
    elif cd:
        bad.append("输出目录的上一级没找到 models 文件夹：" + cd
                   + " → 确认「输出目录」指向的是 ComfyUI 的 output")
    d = CFG.get("doc") or ""
    if d and os.path.isfile(d):
        ok.append("文档")
    else:
        bad.append("文档路径无效：" + (d or "（还没设置）") + " → 用顶部「选择文件…」")
    a = CFG.get("assets") or ""
    if a and os.path.isdir(a):
        ok.append("素材库")
    else:
        bad.append("素材库路径无效：" + (a or "（还没设置）") + " → 用顶部「选择文件夹…」")
    if ok:
        log("自检通过：" + " · ".join(ok))
    for m in bad:
        log("⚠ " + m)


class LocalServer(ThreadingHTTPServer):
    """本程序的 HTTP 服务。"""
    daemon_threads = True

    def handle_error(self, request, client_address):
        """浏览器一关页面/一切片就会掐断连接，这属于正常现象。
        以前每次都在黑窗口里刷一屏 traceback，看着像程序崩了。"""
        et = sys.exc_info()[0]
        if et in (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            return
        super().handle_error(request, client_address)


def _our_server_on(port, timeout=2):
    """端口上的那个服务是不是「本程序的另一个实例」。
    光看 HTTP 200 不够 —— 别的程序也可能占着这个端口返回 200。"""
    try:
        with _OPENER.open("http://127.0.0.1:%d/api/state" % int(port),
                          timeout=timeout) as r:
            if r.getcode() != 200:
                return False
            d = json.load(r)
        return isinstance(d, dict) and ("shots" in d) and ("meta" in d)
    except Exception:
        return False


def _port_busy(port, timeout=0.4):
    """
    端口上是不是真的有东西在监听（能连上就算）。
    Windows 有个坑：两边都开 SO_REUSEADDR 时，bind 会「成功」但连接其实跑到别人那儿去，
    表现为「服务说自己启动了，浏览器却打不开」。所以绑之前先连一下探一探。
    """
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def _open_browser(url):
    try:
        webbrowser.open(url)
    except Exception:
        pass


def main():
    # Windows 控制台（bat 里 chcp 936）可能打不出某些字符。让编码错误降级成 ?
    # 而不是抛异常 —— 日志打不出来是小事，把请求带崩才是大事。
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except Exception:
            pass
    load_config()
    want = int(CFG.get("port") or 8771)
    # 已经有一个在跑：把那个页面直接给你打开，别静悄悄退出
    # （先 _port_busy 探一下，端口没人监听时就不用等 2 秒的 HTTP 探测）
    if _port_busy(want) and _our_server_on(want):
        url = f"http://127.0.0.1:{want}"
        print(f"\n已经有一个在跑了（端口 {want}），这就帮你打开：{url}"
              f"\n（要重启的话，先把那个旧的黑窗口关掉再双击）")
        _open_browser(url)
        raise SystemExit(2)      # bat 认得这个返回码，不会再显示「程序异常退出」

    load_locks()
    load_titles()          # 先读回节点名，否则下面 ensure_mapping 会以为「还没有名字」重新生成
    prune_baks()
    if not os.path.exists(SHOTS_PATH):
        try:
            reparse(keep_state=False)
        except Exception as e:
            print("首次解析失败：", e)
    data = load_shots() or {"shots": []}
    print(f"文档导演台 · {len(data.get('shots', []))} 段")
    print(f"文档：{CFG['doc']}")
    print(f"素材：{CFG['assets']}（{len(list_assets())} 张）")
    print(f"工作流：{CFG['template']}")
    try:
        ensure_mapping()
    except Exception as e:
        print("工作流映射失败：", e)
    print(f"ComfyUI：{CFG['comfy']}  {'在线' if comfy_online() else '离线!'}")
    # 日志轮转：上一次的运行日志留成 .1，方便对照
    try:
        if os.path.isfile(LOG_PATH):
            shutil.copy2(LOG_PATH, LOG_PATH + ".1")
            os.remove(LOG_PATH)
    except Exception:
        pass

    start_ws()
    start_title_watcher()
    selfcheck()
    log(f"启动完成 · 模式：{'只跑一采' if CFG.get('onepass') else '一采+二采'}")

    want = int(CFG.get("port") or 8771)
    # 端口被别的程序占着就往后顺延，不再直接抛 traceback 闪退
    srv, last_err = None, None
    for p in range(want, want + 10):
        if _port_busy(p):
            last_err = f"端口 {p} 上已经有程序在监听"
            log(f"端口 {p} 被别的程序占用，试下一个")
            continue
        try:
            srv = LocalServer(("127.0.0.1", p), Handler)
            want = p
            break
        except OSError as e:
            last_err = e
    if srv is None:
        msg = ("端口 %d–%d 都被别的程序占用，起不来。\n"
               "改一下 config.json 里的 \"port\"（比如 8790），或者关掉占端口的程序。\n"
               "最后一条错误：%s" % (want, want + 9, last_err))
        print("!! " + msg)
        log("!! 端口都被占用，启动失败")
        raise SystemExit(3)
    if want != int(CFG.get("port") or 8771):
        log(f"端口 {CFG.get('port')} 被别的程序占用，本次改用 {want}"
            "（想固定就改 config.json 里的 port）")
        print(f"（{CFG.get('port')} 被占用，改用 {want}）")

    url = f"http://127.0.0.1:{want}"
    _open_browser(url)
    print(f"\n服务已启动：{url}\n（这个黑窗口别关，关掉服务就停了）")
    srv.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        import traceback
        tb = traceback.format_exc()
        print("\n" + tb)
        # 黑窗口一关就什么都看不到了，所以同时存一份到文件
        try:
            with open(os.path.join(HERE, "启动错误.txt"), "w", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n" + tb)
        except Exception:
            pass
        print("启动失败，完整原因已存到：启动错误.txt")
