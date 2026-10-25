"""B站接口封装。

只做三件事：
1. 拿推荐视频（首页推荐流）；
2. 拿视频详情（标题 / 简介 / UP主 / 分区 / 播放量），发评论；
3. 拿消息中心里别人对我的 @ 和评论回复，并回过去。

所有方法都是 async，调用方必须串行调用（本项目刻意不做并发）。
"""
from __future__ import annotations

import json
import logging
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx

from bilibili_api import Picture, comment, homepage, hot, rank, search, session, user, video
from bilibili_api.comment import CommentResourceType, OrderType
from bilibili_api.dynamic import upload_image
from bilibili_api.exceptions import NetworkException, ResponseCodeException
from bilibili_api.utils.network import Api, Credential
from bilibili_api.utils.utils import get_api

from config import Config, app_home
from risk import is_risk

log = logging.getLogger(__name__)

BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")

# 消息中心（回复我的 / @我的）。bilibili-api 的 session.get_replies() 会传 id=None，
# 实测返回空 data，所以这里自己发请求，参数只用 build / mobi_app / platform。
INBOX_URLS = {
    "at": "https://api.bilibili.com/x/msgfeed/at",
    "reply": "https://api.bilibili.com/x/msgfeed/reply",
}

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

SOURCE_NAMES = {
    "recommend": "首页推荐",
    "popular": "热门榜",
    "rank": "排行榜",
    "search": "搜索关键词",
    "mix": "混合",
}


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class VideoInfo:
    bvid: str = ""
    aid: int = 0
    title: str = ""
    desc: str = ""
    up_name: str = ""
    up_mid: int = 0
    tname: str = ""
    duration: int = 0
    view: int = 0
    pubdate: int = 0
    cid: int = 0

    @property
    def short_desc(self) -> str:
        return (self.desc or "").strip().replace("\n", " ")[:400]


@dataclass
class InboxItem:
    """消息中心里的一条 @ 或评论回复。

    字段含义（来自 B站 /x/msgfeed/reply 的真实返回）：
        subject_id  视频 aid（评论接口的 oid）
        root_id     这条通知所在评论楼的根评论 id，0 表示没有楼
        source_id   对方这条新回复的 rpid —— 要回的就是它
        target_id   被回复的那条评论 id（通常是我自己发的评论）
        source_content 对方这条回复的正文
    """
    key: str = ""                 # 去重用的唯一键
    kind: str = ""                # "at" / "reply"
    msg_id: int = 0
    user_name: str = ""
    user_mid: int = 0
    text: str = ""                # 对方说的话（拿不到就是空）
    title: str = ""               # 视频标题
    bvid: str = ""
    aid: int = 0                  # 视频 aid，也就是评论接口的 oid
    root_id: int = 0              # 评论楼根 id，0 = 无
    reply_id: int = 0             # 对方这条回复的 rpid（回复的 parent）
    my_comment_id: int = 0        # 被回复的评论 id（一般是自己的）
    business_id: int = 1          # 1=视频评论，11/17=动态评论
    uri: str = ""
    ts: int = 0                   # 消息时间戳（秒），用来过滤太旧的消息
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_video(self) -> bool:
        return self.business_id == 1 and (self.aid > 0 or bool(self.bvid))

    @property
    def root_parent(self) -> tuple[int, Optional[int]]:
        """换算成 send_comment 的 (root, parent)。

        - 对方在某个评论楼里回复我 → root=root_id, parent=对方的 rpid
        - 对方直接给视频发评论并 @ 我（root_id=0）→ root=对方 rpid, parent=None
        """
        if self.reply_id <= 0:
            return 0, None
        if self.root_id:
            return self.root_id, self.reply_id
        return self.reply_id, None


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------

class BiliClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._cred: Optional[Credential] = None
        self._self_uid: int = 0
        self._self_name: str = ""
        self.master_name: str = ""   # 主人的昵称，resolve_master() 之后才有值

    # -- 凭据 ----------------------------------------------------------
    @property
    def credential(self) -> Credential:
        if self._cred is None:
            cfg = self.cfg
            self._cred = Credential(
                sessdata=(cfg.sessdata or "").strip(),
                bili_jct=(cfg.bili_jct or "").strip(),
                buvid3=(cfg.buvid3 or "").strip() or None,
                buvid4=(cfg.buvid4 or "").strip() or None,
                dedeuserid=(cfg.dedeuserid or "").strip() or None,
            )
        return self._cred

    @property
    def self_uid(self) -> int:
        return self._self_uid

    async def check_login(self) -> dict:
        """校验 Cookie 是否有效，返回自己的信息。"""
        info = await user.get_self_info(self.credential)
        self._self_uid = int(info.get("mid") or 0)
        self._self_name = str(info.get("name") or "")
        return info

    async def resolve_master(self) -> str:
        """按 UID 查出主人昵称（发 @ 时必须用昵称）。没配 UID 就返回空。"""
        uid = (self.cfg.master_uid or "").strip()
        if not uid.isdigit():
            return ""
        info = await user.User(uid=int(uid)).get_user_info()
        self.master_name = str(info.get("name") or "")
        return self.master_name

    # -- 推荐流 --------------------------------------------------------
    # -- 视频来源 ------------------------------------------------------
    async def get_videos(self, limit: int = 15) -> list[VideoInfo]:
        """按配置的来源取一批视频（顺序已经在内部打乱）。"""
        source = (self.cfg.video_source or "recommend").lower()
        keyword = (getattr(self.cfg, "search_keyword", "") or "").strip()

        if source == "mix":
            pool = ["recommend", "popular", "rank"]
            if keyword:
                pool.append("search")
            source = random.choice(pool)
            log.info("混合来源，本轮抽到：%s", SOURCE_NAMES.get(source, source))
        if source == "search":
            return await self.get_search(keyword, limit)
        if source == "popular":
            return await self.get_popular(limit)
        if source == "rank":
            return await self.get_rank(limit)
        return await self.get_recommend(limit)

    async def get_search(self, keyword: str, limit: int = 15) -> list[VideoInfo]:
        """按关键词搜索视频；关键词为空就退回首页推荐（默认行为）。"""
        keyword = (keyword or "").strip()
        if not keyword:
            log.warning("没有填搜索关键词，自动退回「首页推荐」")
            return await self.get_recommend(limit)

        data = await search.search_by_type(
            keyword, search_type=search.SearchObjectType.VIDEO, page=1
        )
        items = (data or {}).get("result") or []
        out: list[VideoInfo] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            bvid = str(it.get("bvid") or "")
            if not BV_RE.fullmatch(bvid):
                continue
            out.append(
                VideoInfo(
                    bvid=bvid,
                    aid=_to_int(it.get("aid")),
                    title=_clean_text_html(str(it.get("title") or "")),
                    desc=_clean_text_html(str(it.get("description") or it.get("desc") or "")),
                    up_name=str(it.get("author") or ""),
                    up_mid=_to_int(it.get("mid")),
                    duration=_parse_duration(it.get("duration")),
                    view=_to_int(it.get("play")),
                    pubdate=_to_int(it.get("pubdate")),
                )
            )
        random.shuffle(out)
        log.info("搜索「%s」拿到 %d 条结果", keyword, len(out))
        return out[:limit]

    async def get_recommend(self, limit: int = 15) -> list[VideoInfo]:
        data = await homepage.get_videos(self.credential)
        items = (data or {}).get("item") or []
        out: list[VideoInfo] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            if it.get("goto") not in (None, "av"):  # 直播/番剧/课程/广告直接跳过
                continue
            brief = self._brief(it)
            if BV_RE.fullmatch(brief.bvid):
                out.append(brief)
        # 推荐流本身每次都不一样，这里再打乱一次，避免总挑到同一批头部视频
        random.shuffle(out)
        return out[:limit]

    async def get_popular(self, limit: int = 15) -> list[VideoInfo]:
        """热门榜：全站热门，不跟个人口味走。"""
        data = await hot.get_hot_videos(pn=1, ps=max(20, min(50, limit * 2)))
        items = (data or {}).get("list") or []
        out = [self._brief(it) for it in items if isinstance(it, dict)]
        out = [v for v in out if BV_RE.fullmatch(v.bvid)]
        random.shuffle(out)
        return out[:limit]

    async def get_rank(self, limit: int = 15) -> list[VideoInfo]:
        """排行榜（三天榜，全站 100 条），随机取。"""
        data = await rank.get_rank(rank.RankType.All, rank.RankDayType.THREE_DAY)
        items = (data or {}).get("list") or []
        out = [self._brief(it) for it in items if isinstance(it, dict)]
        out = [v for v in out if BV_RE.fullmatch(v.bvid)]
        random.shuffle(out)
        return out[:limit]

    def _brief(self, it: dict) -> VideoInfo:
        owner = it.get("owner") or {}
        stat = it.get("stat") or {}
        return VideoInfo(
            bvid=str(it.get("bvid") or ""),
            aid=_to_int(it.get("aid")),  # 注意：推荐流里的 id 不是 aid
            title=str(it.get("title") or ""),
            desc=str(it.get("desc") or ""),
            up_name=str(owner.get("name") or ""),
            up_mid=_to_int(owner.get("mid")),
            tname=str(it.get("tname") or it.get("tnamev2") or ""),
            duration=_to_int(it.get("duration")),
            view=_to_int(stat.get("view")),
            pubdate=_to_int(it.get("pubdate")),
            cid=_to_int(it.get("cid")),
        )

    # -- 视频详情 ------------------------------------------------------
    async def get_video_info(self, bvid: str) -> VideoInfo:
        v = video.Video(bvid=bvid, credential=self.credential)
        info = await v.get_info()
        owner = info.get("owner") or {}
        stat = info.get("stat") or {}
        pages = info.get("pages") or []
        cid = int(info.get("cid") or (pages[0].get("cid") if pages else 0) or 0)
        return VideoInfo(
            bvid=str(info.get("bvid") or bvid),
            aid=int(info.get("aid") or 0),
            title=str(info.get("title") or ""),
            desc=str(info.get("desc") or ""),
            up_name=str(owner.get("name") or ""),
            up_mid=int(owner.get("mid") or 0),
            tname=str(info.get("tname") or info.get("tname_v2") or ""),
            duration=int(info.get("duration") or 0),
            view=int(stat.get("view") or 0),
            pubdate=int(info.get("pubdate") or 0),
            cid=cid,
        )

    # -- 内容理解（可选，让评论更贴内容）--------------------------------
    async def get_top_comments(self, aid: int, limit: int = 5) -> list[str]:
        """按点赞排序取前几条热评，给模型当“这视频在聊什么”的参考。"""
        if not aid:
            return []
        data = await comment.get_comments(
            oid=aid,
            type_=CommentResourceType.VIDEO,
            page_index=1,
            order=OrderType.LIKE,
            credential=self.credential,
        )
        replies = (data or {}).get("replies") or []
        out: list[str] = []
        for r in replies:
            msg = str(((r.get("content") or {}).get("message")) or "").strip()
            msg = re.sub(r"\s+", " ", msg)
            if len(msg) < 4:
                continue
            out.append(msg)
            if len(out) >= limit:
                break
        return out

    async def get_subtitle_text(self, bvid: str, aid: int, cid: int, max_chars: int = 600) -> str:
        """有 CC / AI 字幕就取一段，没有就返回空字符串。"""
        if not cid:
            return ""
        v = video.Video(bvid=bvid, credential=self.credential)
        if aid:
            v.set_aid(int(aid))  # 提前塞进去，免得为了拿 aid 再发一次请求
        info = await v.get_subtitle(cid=cid)
        subs = (info or {}).get("subtitles") or []
        if not subs:
            return ""
        url = str(subs[0].get("subtitle_url") or "")
        if not url:
            return ""
        if url.startswith("//"):
            url = "https:" + url

        try:
            cookies = {k: v for k, v in self.credential.get_cookies().items() if v}
        except Exception:
            cookies = {}
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(
                url,
                headers={"User-Agent": UA, "Referer": "https://www.bilibili.com"},
                cookies=cookies,
            )
            resp.raise_for_status()
            data = resp.json()

        body = data.get("body") or []
        text = " ".join(str(x.get("content") or "").strip() for x in body if isinstance(x, dict))
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars]

    # -- 发评论 --------------------------------------------------------
    async def post_comment(
        self,
        info: VideoInfo,
        text: str,
        mention: Optional[tuple[str, int]] = None,
        picture: Optional[Path] = None,
    ) -> dict:
        """发评论；mention 会带真 @，picture 是本地图片（作为带图评论发出去）。"""
        if not info.aid:
            raise ValueError(f"视频 {info.bvid} 缺少 aid，无法评论")
        return await self._send(text=text, oid=info.aid, mention=mention, picture=picture)

    async def reply(self, item: InboxItem, text: str, mention: Optional[tuple[str, int]] = None) -> dict:
        if not item.aid:
            raise ValueError(f"消息 {item.key} 缺少 aid，无法回复")
        root, parent = item.root_parent
        return await self._send(text=text, oid=item.aid, root=root or None, parent=parent, mention=mention)

    async def _send(
        self,
        text: str,
        oid: int,
        root: Optional[int] = None,
        parent: Optional[int] = None,
        mention: Optional[tuple[str, int]] = None,
        picture: Optional[Path] = None,
    ) -> dict:
        """发评论。

        mention=(昵称, UID) 时带上真正的 @（会通知对方），picture=本地图片时随评论发图。
        bilibili-api 的 send_comment 没暴露 at_name_to_mid / pictures 的手动组装，
        所以带 @ 或带图时这里自己拼一次请求；两者都没有时仍走库里实现，少一份维护成本。
        """
        if mention is None and picture is None:
            return await comment.send_comment(
                text=text,
                oid=oid,
                type_=CommentResourceType.VIDEO,
                root=root,
                parent=parent,
                credential=self.credential,
            )

        # 走的是自己拼的请求，把库里的两项检查补上
        self.credential.raise_for_no_sessdata()
        self.credential.raise_for_no_bili_jct()

        api = get_api("common")["comment"]["send"]
        data: dict[str, Any] = {
            "oid": oid,
            "type": CommentResourceType.VIDEO.value,
            "message": text,
            "plat": 1,
            "statistics": {"appId": 100, "platform": 5},
            "gaia_source": "main_web",
        }

        name, uid = mention or ("", 0)
        name = (name or "").strip().lstrip("@")
        if name and uid:
            # B站要求：message 里出现 @昵称，同时用这个字段把昵称映射到 mid，
            # 才会渲染成真正的艾特并给对方发通知
            data["at_name_to_mid"] = json.dumps({name: int(uid)}, ensure_ascii=False)
        if picture is not None:
            data["pictures"] = await self._upload_picture(picture)
        if root is not None:
            data["root"] = int(root)
            data["parent"] = int(root if parent is None else parent)
        return await Api(**api, credential=self.credential).update_data(**data).result

    async def _upload_picture(self, path: Path) -> str:
        """把本地图片传到 B站图床，返回评论接口要的 pictures JSON 字符串。"""
        pic = Picture.from_file(str(path))
        res = await upload_image(image=pic, credential=self.credential)
        url = str((res or {}).get("image_url") or "")
        if not url:
            raise ValueError("B站图床没有返回图片地址")
        return json.dumps(
            [
                {
                    "img_src": url,
                    "img_width": _to_int((res or {}).get("image_width"), pic.width),
                    "img_height": _to_int((res or {}).get("image_height"), pic.height),
                    "img_size": _to_int((res or {}).get("img_size"), int(pic.size or 0)),
                }
            ],
            ensure_ascii=False,
        )

    # -- 消息中心 ------------------------------------------------------
    async def find_recent_comment_rpid(self, aid: int, user_mid: int, text: str = "") -> int:
        """兜底：消息里没带 rpid 时，去视频最新评论里找这个人刚发的那条。"""
        data = await comment.get_comments(
            oid=aid,
            type_=CommentResourceType.VIDEO,
            page_index=1,
            order=OrderType.TIME,
            credential=self.credential,
        )
        replies = (data or {}).get("replies") or []
        needle = (text or "").strip()
        for r in replies[:20]:
            if _to_int(r.get("mid")) != user_mid:
                continue
            msg = str(((r.get("content") or {}).get("message")) or "").strip()
            if not needle or needle in msg or msg in needle:
                return _to_int(r.get("rpid"))
        return 0

    async def fetch_inbox(self) -> list[InboxItem]:
        out: list[InboxItem] = []
        for kind in ("at", "reply"):
            try:
                data = await self._fetch_feed(kind)
            except Exception as exc:  # 单个来源失败不影响另一个；风控要往上报
                if is_risk(exc):
                    raise
                log.warning("拉取 %s 消息失败：%s", kind, exc)
                continue
            out.extend(self._parse_msgfeed(kind, data))
        return out

    # -- 私信 ----------------------------------------------------------
    async def fetch_dm_sessions(self) -> list[dict]:
        data = await session.get_sessions(self.credential, session_type=4)
        return (data or {}).get("session_list") or []

    async def fetch_dm_messages(self, talker_id: int, limit: int = 10) -> list[dict]:
        data = await session.fetch_session_msgs(
            talker_id=talker_id, credential=self.credential, session_type=4, begin_seqno=0
        )
        msgs = (data or {}).get("messages") or []
        return msgs[-limit:]

    async def send_dm(self, talker_id: int, text: str) -> dict:
        return await session.send_msg(
            self.credential, talker_id, session.EventType.TEXT, text
        )

    async def _fetch_feed(self, kind: str) -> dict:
        """直接打消息中心接口（库里的封装会传坏参数，返回空）。"""
        cookies = {k: v for k, v in self.credential.get_cookies().items() if v}
        async with httpx.AsyncClient(
            timeout=20.0,
            headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"},
        ) as client:
            resp = await client.get(
                INBOX_URLS[kind],
                params={"build": 0, "mobi_app": "web", "platform": "web"},
                cookies=cookies,
            )
        if resp.status_code != 200:
            raise NetworkException(resp.status_code, resp.text[:200])
        payload = resp.json()
        code = int(payload.get("code") or 0)
        if code != 0:
            raise ResponseCodeException(code, str(payload.get("message") or ""), payload)
        return payload

    def _parse_msgfeed(self, kind: str, data: Any) -> list[InboxItem]:
        items = ((data or {}).get("data") or {}).get("items") or []
        out: list[InboxItem] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            item = it.get("item") or {}
            who = it.get("user") or {}
            msg_id = _to_int(it.get("id"))

            business_id = _to_int(item.get("business_id") or item.get("business"), 1)
            aid = _to_int(item.get("subject_id") or item.get("oid"))
            uri = str(item.get("uri") or item.get("native_uri") or "")
            bvid = _find_bvid(uri) or _find_bvid(str(item.get("title") or ""))

            text = _first_str(
                item.get("source_content"),          # 对方这条回复的正文
                item.get("target_reply_content"),    # 被回复的（我的）内容，兜底
                item.get("root_reply_content"),
                item.get("content"),
                it.get("source_content"),
                it.get("content"),
                it.get("reply_content"),
            )
            title = str(item.get("title") or "")

            reply_id = _to_int(item.get("source_id"))
            my_comment_id = _to_int(item.get("target_id"))

            key = f"{kind}:{msg_id}" if msg_id else f"{kind}:{aid}:{reply_id}"
            out.append(
                InboxItem(
                    key=key,
                    kind=kind,
                    msg_id=msg_id,
                    user_name=str(who.get("nickname") or who.get("name") or ""),
                    user_mid=_to_int(who.get("mid")),
                    text=text,
                    title=title,
                    bvid=bvid,
                    aid=aid,
                    root_id=_to_int(item.get("root_id")),
                    reply_id=reply_id,
                    my_comment_id=my_comment_id,
                    business_id=business_id,
                    uri=uri,
                    ts=_to_int(it.get("at_time") or it.get("reply_time") or it.get("time")),
                    raw=it,
                )
            )

            if not aid and business_id == 1:
                _dump_unknown(it)

        return out


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _find_bvid(text: str) -> str:
    m = BV_RE.search(text or "")
    return m.group(0) if m else ""


def dm_text(msg: dict) -> str:
    """私信 content 是个 JSON 字符串，取出里面的正文。"""
    raw = msg.get("content") or ""
    if not isinstance(raw, str):
        return ""
    try:
        data = json.loads(raw)
    except Exception:
        return raw[:200]
    if isinstance(data, dict):
        return str(data.get("content") or data.get("text") or "")[:200]
    return str(data)[:200]


def _clean_text_html(text: str) -> str:
    """搜索结果标题里带 <em class="keyword"> 高亮，去掉标签并还原实体。"""
    import html as _html

    return _html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def _parse_duration(value: Any) -> int:
    """把 "18:02" / "1:02:03" 这种时长转成秒。"""
    raw = str(value or "").strip()
    if not raw:
        return 0
    parts = raw.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    seconds = 0
    for n in nums:
        seconds = seconds * 60 + n
    return seconds


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _first_str(*values: Any) -> str:
    for v in values:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _dump_unknown(item: dict) -> None:
    """把解析不出 aid 的消息原样存下来，方便反馈问题。"""
    try:
        p: Path = logs_dir_path() / "inbox_raw.jsonl"
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    except Exception:
        pass


def logs_dir_path() -> Path:
    d = app_home() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d
