"""表情包：meme/ 目录里的图片，文件名就是情绪标签。

    meme/开心.png
    meme/生气_2.jpg     ← 同一个情绪的多张图会自动归到「生气」
    meme/哈哈哈.gif

评论生成后按 config.meme_chance 掷一次骰子，命中就随机挑一张，先传到 B站图床，
再作为「带图评论」发出去（B站的评论接口支持 pictures 字段）。

找图顺序（同名文件以先找到的为准）：
    1. exe 旁边的 meme/            （源码运行时就是项目目录）
    2. %APPDATA%\\BiliFish\\meme
两处都没有图片时这个功能自动跳过，不影响正常发评论。
"""
from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from config import app_home, app_root

log = logging.getLogger(__name__)

DIRNAME = "meme"
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
# 「开心_1」「开心(2)」「开心-3」都归到「开心」
_INDEX_RE = re.compile(r"[ _\-(（]?\d+[)）]?$")


@dataclass(frozen=True)
class Meme:
    """一张表情包。tag 是情绪名（文件名去掉扩展名和序号）。"""

    path: Path
    tag: str

    @property
    def name(self) -> str:
        return self.path.name


def candidate_dirs() -> list[Path]:
    """按优先级返回可能放表情包的目录。"""
    out: list[Path] = []
    for base in (app_root(), app_home()):
        p = base / DIRNAME
        if p not in out:
            out.append(p)
    return out


def ensure_dir() -> Path:
    """确保有一个能用的 meme 目录，返回它（GUI 的「打开表情包文件夹」用）。"""
    for p in candidate_dirs():
        try:
            p.mkdir(parents=True, exist_ok=True)
            return p
        except Exception:
            continue
    return candidate_dirs()[0]


def tag_of(path: Path) -> str:
    """文件名 → 情绪名：去掉扩展名和结尾的序号。"""
    stem = path.stem.strip()
    stem = _INDEX_RE.sub("", stem).strip(" _-")
    return stem or path.stem


def scan() -> list[Meme]:
    """扫描所有候选目录，返回可用表情包（同名文件只取第一个目录里的）。"""
    out: list[Meme] = []
    seen: set[str] = set()
    for d in candidate_dirs():
        if not d.is_dir():
            continue
        try:
            files = sorted(d.iterdir())
        except Exception as exc:  # 权限不够之类，别让评论流程挂掉
            log.debug("扫描 %s 失败：%s", d, exc)
            continue
        for f in files:
            try:
                if not f.is_file() or f.name.startswith("."):
                    continue
                if f.suffix.lower() not in IMAGE_EXTS:
                    continue
            except OSError:
                continue
            if f.name in seen:
                continue
            seen.add(f.name)
            out.append(Meme(path=f, tag=tag_of(f)))
    return out


def tags(memes: list[Meme]) -> list[str]:
    return sorted({m.tag for m in memes})


def pick(
    memes: list[Meme],
    chance: int,
    rng: random.Random | None = None,
    avoid: str = "",
) -> Optional[Meme]:
    """按概率挑一张；avoid 是上一张的路径字符串，尽量不连着用同一张。"""
    rng = rng or random
    if not memes or int(chance) <= 0:
        return None
    if rng.randint(1, 100) > int(chance):
        return None
    pool = [m for m in memes if str(m.path) != avoid] or memes
    return rng.choice(pool)


def help_text() -> str:
    """界面「格式说明」里关于表情包的那一段。"""
    folders = "\n".join(f"  {p}" for p in candidate_dirs())
    return "\n".join(
        [
            "表情包放在这个文件夹里，文件名就是情绪标签：",
            "",
            "  meme/开心.png",
            "  meme/生气_2.jpg",
            "  meme/哈哈哈.gif",
            "",
            "规则：",
            "  • 支持 png / jpg / jpeg / webp / gif / bmp；",
            "  • 同一个情绪可以放多张，用「开心_1」「开心(2)」这类序号区分，会归到同一个情绪；",
            "  • 每次评论按「表情包概率」独立掷骰子，命中就随机挑一张，跟文字一起发成带图评论；",
            "  • 图片会先传到 B站图床；如果账号发不了图，这条会退回纯文字并在日志里说明；",
            "  • 没有图片时这个功能自动跳过。",
            "",
            "找图目录（同名文件以先找到的为准）：",
            folders,
        ]
    )
