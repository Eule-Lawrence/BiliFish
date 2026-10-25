"""预制评论模板：一行一条，可以写占位符。

支持的占位符（不写也行，纯文字照样能用）：

    {owner}    主人（发出去会变成真的 @昵称，对方能收到提醒）
    {up}       UP主昵称
    {title}    视频标题
    {section}  分区名（例如「生活区」）

例：

    {owner}你来看看这个视频！
    {up}这期拍得也太顶了
    {title}，我先码住
    {section}也能出这种活啊

命中概率由 config.preset_chance 控制（0 = 不用）；命中后只会挑一条交给 AI，
让它学着句式和语气改写成属于当前视频的评论，不会照搬。

注意 `{owner}` 在喂给模型时会写成 `@主人` 这个记号，模型输出后由
runner._apply_master_mention() 换成真实昵称并发真正的 @。
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Optional

MAX_PRESETS = 8          # 一次最多解析几条（和人设页提示保持一致）
MAX_PROMPT_CHARS = 120   # 单条预制评论进 prompt 前截断

# 占位符说明，界面里的「格式说明」和 README 都用同一份文字
PLACEHOLDER_HELP: dict[str, str] = {
    "owner": "主人（换成真的 @昵称，对方能收到提醒；没配主人 UID 时降级成「我主人」）",
    "up": "UP主昵称",
    "title": "视频标题（超过 30 字会截断）",
    "section": "分区名，例如「生活区」",
}

# 给模型看的 @ 记号：模型原样保留，发出去之前换成真实昵称
OWNER_MARK = "@主人"
_LEGACY_OWNER = "@主人"

_KNOWN_RE = re.compile(r"\{(owner|up|title|section)\}")
_UNKNOWN_RE = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}")


@dataclass(frozen=True)
class PresetTemplate:
    """一行预制评论。"""

    raw: str

    @property
    def uses_owner(self) -> bool:
        """这条模板要不要 @ 主人。"""
        return "{owner}" in self.raw or _LEGACY_OWNER in self.raw

    @property
    def unknown_placeholders(self) -> list[str]:
        """写错的占位符（比如 {name}），只用来提醒，不阻断。"""
        return _UNKNOWN_RE.findall(_KNOWN_RE.sub("", self.raw))

    def render(self, *, up_name: str = "", title: str = "", section: str = "") -> str:
        """把占位符换成真值，返回喂给模型的那一版。

        {owner} 留成 `@主人` 记号，由 runner 在最终文本里换成真实昵称。
        """
        text = self.raw.replace("{owner}", OWNER_MARK)
        text = text.replace("{up}", _clip(up_name, 20) or "UP主")
        text = text.replace("{title}", _clip(title, 30) or "这条视频")
        text = text.replace("{section}", _clip(section, 12) or "这个分区")
        return re.sub(r"\s+", " ", text).strip()


def parse_presets(raw: str, limit: int = MAX_PRESETS) -> list[PresetTemplate]:
    """把多行文本解析成模板列表：一行一条，`#` 开头的行当注释跳过。"""
    out: list[PresetTemplate] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("- "):
            line = line[2:].strip()
        if not line:
            continue
        out.append(PresetTemplate(line))
        if len(out) >= limit:
            break
    return out


def pick_preset(
    templates: list[PresetTemplate],
    chance: int,
    rng: random.Random | None = None,
    avoid: str = "",
) -> Optional[PresetTemplate]:
    """按概率抽一条预制评论；没命中返回 None。

    avoid 是上一次用过的 raw 文本，尽量不连着抽到同一条。
    """
    rng = rng or random
    if not templates or int(chance) <= 0:
        return None
    if rng.randint(1, 100) > int(chance):
        return None
    pool = [t for t in templates if t.raw != avoid] or templates
    return rng.choice(pool)


def help_text() -> str:
    """界面「格式说明」用的一整段文字。"""
    lines = ["预制评论一行一条，可以混着写纯文字和占位符。", "", "可用的占位符："]
    for name, desc in PLACEHOLDER_HELP.items():
        lines.append(f"  {{{name}}}    {desc}")
    lines += [
        "",
        "例子：",
        "  {owner}你来看看这个视频！",
        "  {up}这期拍得也太顶了，蹲个后续",
        "  {title}，我先码住慢慢看",
        "  {section}也能整这种活啊",
        "",
        "说明：",
        "  • 每次评论按「预制评论概率」独立掷一次骰子，命中才会挑一条；",
        "  • AI 只学它的句式和语气，会结合当前视频改写成自己的话，不会照抄；",
        "  • 没命中时不会把预制评论喂给 AI，让它自由发挥；",
        "  • {owner} 会变成真正的 @昵称（需要填「主人 UID」），对方能收到提醒；",
        "  • 写错的占位符（例如 {name}）会原样留在评论里，记得只用上面四个。",
    ]
    return "\n".join(lines)


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text[:limit] if len(text) > limit else text
