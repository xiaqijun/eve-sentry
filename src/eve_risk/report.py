from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from eve_risk.analysis import SHANGHAI
from eve_risk.domain import (
    AnalysisReport,
    AssociateCandidate,
    CharacterProfile,
    CompositionMetric,
    Confidence,
    LatestEngagement,
    NamedMetric,
)

BG = "#07111f"
PANEL = "#0e2035"
PANEL_ALT = "#132942"
TEXT = "#edf6ff"
MUTED = "#8fa9bf"
CYAN = "#49b6ff"
GREEN = "#59d499"
YELLOW = "#f4ca64"
ORANGE = "#ff9f43"
RED = "#ff5f6d"
PURPLE = "#b084ff"
GRID = "#26415c"

ROLE_COLORS = {
    "输出舰": RED,
    "后勤": GREEN,
    "电子战": PURPLE,
    "抓人": ORANGE,
    "拦截": YELLOW,
    "指挥": CYAN,
    "侦察": "#65d8d0",
    "旗舰": "#d28cff",
    "工业": MUTED,
    "其他": MUTED,
}


class ReportRenderer:
    def __init__(
        self, width: int = 1440, max_height: int = 4096, font_path: str | None = None
    ) -> None:
        self.width = width
        self.max_height = max_height
        self.font_path = _find_font(font_path)
        self.fonts = {
            "title": ImageFont.truetype(self.font_path, 52),
            "score": ImageFont.truetype(self.font_path, 46),
            "h2": ImageFont.truetype(self.font_path, 32),
            "h3": ImageFont.truetype(self.font_path, 27),
            "body": ImageFont.truetype(self.font_path, 24),
            "small": ImageFont.truetype(self.font_path, 20),
            "tiny": ImageFont.truetype(self.font_path, 17),
        }

    def render(self, report: AnalysisReport) -> bytes:
        profile_count = min(4, len(report.profiles))
        height = self.max_height
        image = Image.new("RGB", (self.width, height), BG)
        draw = ImageDraw.Draw(image)
        margin = 64
        content_width = self.width - margin * 2
        y = 48

        draw.text((margin, y), "EVE 敌对舰队情报", fill=TEXT, font=self.fonts["title"])
        generated = report.generated_at.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M CST")
        draw.text(
            (self.width - margin, y + 8),
            generated,
            fill=MUTED,
            font=self.fonts["small"],
            anchor="ra",
        )
        subtitle = (
            f"有效角色 {report.resolved_count}/{report.requested_count}  ·  "
            f"公开事件 {report.data_events} / 交战 {report.engagement_count}  ·  "
            f"数据覆盖 {report.coverage_ratio:.0%}  ·  "
            "Tranquility / 近90天"
        )
        draw.text((margin, y + 62), subtitle, fill=MUTED, font=self.fonts["small"])
        y += 108

        y = self._kpis(draw, report, margin, y, content_width)
        y = self._section_title(draw, y, "最近三场来犯")
        previous_engagements = [
            item for item in report.recent_engagements if item != report.latest_engagement
        ][:2]
        history_count = len(previous_engagements)
        latest_base_height = 220 if report.common_associates else 160
        latest_height = latest_base_height + (42 + history_count * 52 if history_count else 0)
        latest_panel = (margin, y, self.width - margin, y + latest_height)
        self._panel(draw, latest_panel)
        self._latest_engagement(
            draw,
            latest_panel,
            report.latest_engagement,
            previous_engagements,
            report.common_associates,
            report.generated_at,
        )
        y += latest_height + 30

        y = self._section_title(draw, y, "舰队模板与常用舰船")
        gap = 24
        left_width = 760
        left = (margin, y, margin + left_width, y + 408)
        right = (margin + left_width + gap, y, self.width - margin, y + 408)
        self._panel(draw, left)
        self._panel(draw, right)
        self._doctrines(draw, left, report)
        self._bars(
            draw,
            right,
            report.top_ships[:5],
            "常用舰船",
            CYAN,
            max_rows=5,
        )
        y += 438

        y = self._section_title(draw, y, "典型舰队构成")
        role_panel = (margin, y, self.width - margin, y + 250)
        self._panel(draw, role_panel)
        self._role_bars(draw, role_panel, report.role_distribution[:9])
        y += 280

        y = self._section_title(draw, y, "活跃热力图（北京时间）")
        activity_panel = (margin, y, self.width - margin, y + 438)
        self._panel(draw, activity_panel)
        self._week_heatmap(draw, activity_panel, report.activity_week_hours, report.activity_hours)
        y += 468

        y = self._section_title(draw, y, "活动区域与威胁构成")
        left = (margin, y, self.width // 2 - 12, y + 430)
        right = (self.width // 2 + 12, y, self.width - margin, y + 430)
        self._panel(draw, left)
        self._panel(draw, right)
        self._locations(draw, left, report)
        self._threat_components(draw, right, report)
        y += 460

        if profile_count:
            y = self._section_title(draw, y, "关键人物画像")
            y = self._profile_cards(draw, margin, y, report.profiles[:4])

        if y + 220 < height:
            y = self._section_title(draw, y, "关键结论")
            bottom = min(height - 36, y + 144)
            draw.rounded_rectangle((margin, y, self.width - margin, bottom), 18, fill=PANEL)
            self._evidence(draw, margin, y, report)
            y = bottom + 36

        # 报告内容数量会随角色数变化，输出前收紧画布，避免手机查看时出现大片空白。
        image = image.crop((0, 0, self.width, min(height, max(720, y))))

        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)
        return output.getvalue()

    def _kpis(
        self,
        draw: ImageDraw.ImageDraw,
        report: AnalysisReport,
        margin: int,
        y: int,
        content_width: int,
    ) -> int:
        gap = 18
        card_width = (content_width - gap * 3) // 4
        threat_color = _threat_color(report.threat_score)
        last = _relative_time(report.last_activity, report.generated_at)
        isk_ratio = _format_isk_ratio(report.destroyed_value_30d, report.lost_value_30d)
        isk_detail = (
            f"摧毁 {_format_isk_short(report.destroyed_value_30d)} · "
            f"损失 {_format_isk_short(report.lost_value_30d)}"
        )
        filled_stars = max(0, min(5, (report.threat_score + 9) // 20))
        stars = "★" * filled_stars + "☆" * (5 - filled_stars)
        cards = [
            (
                "敌对评级",
                f"{stars} {report.threat_level}",
                f"威胁指数 {report.threat_score} / 100",
                threat_color,
            ),
            ("活跃时间", report.peak_activity, f"最近遭遇 {last}", ORANGE),
            (
                "常用舰队规模",
                report.fleet_size_label,
                (
                    f"通常约 {report.median_gang_size:.0f} 人 · 大场约 {report.p75_gang_size:.0f} 人"
                    if report.median_gang_size is not None and report.p75_gang_size is not None
                    else "公开样本不足"
                ),
                PURPLE,
            ),
            (
                "近30天 ISK战损",
                isk_ratio,
                isk_detail,
                _isk_ratio_color(report),
            ),
        ]
        for index, (label, value, detail, color) in enumerate(cards):
            x = margin + index * (card_width + gap)
            draw.rounded_rectangle((x, y, x + card_width, y + 176), 18, fill=PANEL)
            draw.rectangle((x, y, x + 6, y + 176), fill=color)
            draw.text((x + 24, y + 18), label, fill=MUTED, font=self.fonts["small"])
            if index == 0:
                draw.text((x + 24, y + 69), stars, fill=color, font=self.fonts["h3"])
                draw.text(
                    (x + card_width - 22, y + 56),
                    report.threat_level,
                    fill=color,
                    font=self.fonts["score"],
                    anchor="ra",
                )
            else:
                value_font = self.fonts["score"]
                if draw.textbbox((0, 0), value, font=value_font)[2] > card_width - 48:
                    value_font = self.fonts["h3"]
                draw.text(
                    (x + 24, y + 56),
                    _fit_text(draw, value, value_font, card_width - 48),
                    fill=color,
                    font=value_font,
                )
            draw.text((x + 24, y + 132), _clip(detail, 25), fill=TEXT, font=self.fonts["tiny"])
        return y + 210

    def _doctrines(
        self, draw: ImageDraw.ImageDraw, bounds: tuple[int, int, int, int], report: AnalysisReport
    ) -> None:
        left, top, right, _ = bounds
        draw.text((left + 26, top + 20), "舰队模板", fill=MUTED, font=self.fonts["small"])
        if not report.doctrines:
            draw.text((left + 26, top + 76), "未识别出稳定体系", fill=TEXT, font=self.fonts["h3"])
            draw.text(
                (left + 26, top + 120),
                "暂未发现重复出现的固定组合",
                fill=MUTED,
                font=self.fonts["small"],
            )
            return
        y = top + 62
        for index, doctrine in enumerate(report.doctrines[:2]):
            color = ORANGE if index == 0 else CYAN
            draw.rounded_rectangle((left + 22, y, right - 22, y + 132), 12, fill=PANEL_ALT)
            draw.text((left + 40, y + 14), doctrine.name, fill=TEXT, font=self.fonts["h3"])
            draw.text(
                (right - 40, y + 14),
                f"{doctrine.confidence}%",
                fill=color,
                font=self.fonts["h3"],
                anchor="ra",
            )
            draw.text(
                (right - 40, y + 53),
                f"{doctrine.encounter_count} / {doctrine.sample_count} 场",
                fill=MUTED,
                font=self.fonts["tiny"],
                anchor="ra",
            )
            evidence = doctrine.evidence[:2] or ["舰船样本不足"]
            line_y = y + 58
            for item in evidence:
                draw.ellipse((left + 40, line_y + 7, left + 48, line_y + 15), fill=color)
                draw.text(
                    (left + 58, line_y),
                    _clip(item, 38),
                    fill=TEXT,
                    font=self.fonts["tiny"],
                )
                line_y += 31
            y += 146

    def _latest_engagement(
        self,
        draw: ImageDraw.ImageDraw,
        bounds: tuple[int, int, int, int],
        engagement: LatestEngagement | None,
        previous_engagements: list[LatestEngagement],
        associates: list[AssociateCandidate],
        generated_at: datetime,
    ) -> None:
        left, top, right, _ = bounds
        if engagement is None:
            draw.text(
                (left + 26, top + 30), "暂无可用进攻编队样本", fill=TEXT, font=self.fonts["h3"]
            )
            draw.text(
                (left + 26, top + 76),
                "公开战报未观察到完整舰队配置",
                fill=MUTED,
                font=self.fonts["small"],
            )
            return

        location = (
            f"{engagement.system_name} · {engagement.region_name}"
            if engagement.region_name
            else engagement.system_name
        )
        draw.text((left + 26, top + 18), _clip(location, 30), fill=TEXT, font=self.fonts["h3"])
        draw.text(
            (right - 26, top + 23),
            _engagement_time_label(engagement.last_seen, generated_at),
            fill=MUTED,
            font=self.fonts["small"],
            anchor="ra",
        )

        outcome_color = {
            "参与击毁": GREEN,
            "交火并有损失": ORANGE,
        }.get(engagement.outcome, CYAN)
        draw.text(
            (left + 26, top + 57), engagement.outcome, fill=outcome_color, font=self.fonts["small"]
        )
        draw.text(
            (left + 150, top + 59),
            _clip(engagement.result_detail, 22),
            fill=TEXT,
            font=self.fonts["tiny"],
        )
        value_text = _engagement_value_text(engagement)
        if value_text:
            value_text += f" · {engagement.destroyed_count} 艘"
            draw.text(
                (left + 430, top + 59),
                value_text,
                fill=YELLOW,
                font=self.fonts["tiny"],
            )
        confidence_color = _confidence_color(engagement.composition_confidence)
        draw.text(
            (right - 26, top + 59),
            _composition_status(engagement),
            fill=confidence_color,
            font=self.fonts["tiny"],
            anchor="ra",
        )

        configuration_label = (
            "同战报舰船"
            if engagement.composition_confidence == Confidence.LOW
            else "稳定配置"
        )
        draw.text(
            (left + 26, top + 111),
            configuration_label,
            fill=MUTED,
            font=self.fonts["tiny"],
        )
        ships = engagement.ships[:5]
        gap = 12
        chip_left = left + 150
        chip_right = right - 26
        chip_width = (chip_right - chip_left - gap * 4) // 5
        for index, ship in enumerate(ships):
            x = chip_left + index * (chip_width + gap)
            color = ROLE_COLORS.get(ship.role, CYAN)
            draw.rounded_rectangle((x, top + 100, x + chip_width, top + 146), 11, fill=PANEL_ALT)
            draw.rectangle((x, top + 100, x + 5, top + 146), fill=color)
            draw.text(
                (x + 16, top + 111),
                _fit_text(
                    draw,
                    f"{ship.name} ×{ship.count}",
                    self.fonts["tiny"],
                    chip_width - 28,
                ),
                fill=TEXT,
                font=self.fonts["tiny"],
            )

        if associates:
            draw.line((left + 26, top + 160, right - 26, top + 160), fill=GRID, width=1)
            draw.text((left + 26, top + 179), "常见同行", fill=MUTED, font=self.fonts["tiny"])
            teammate_x = left + 132
            for associate in associates[:3]:
                relation = "固定" if associate.relation_label == "固定队友" else "同行"
                label = f"{_clip(associate.name, 16)} · {relation} · {associate.engagement_count}场"
                text_width = draw.textbbox((0, 0), label, font=self.fonts["tiny"])[2]
                chip_width = text_width + 28
                if teammate_x + chip_width > right - 26:
                    break
                draw.rounded_rectangle(
                    (teammate_x, top + 172, teammate_x + chip_width, top + 206),
                    10,
                    fill=PANEL_ALT,
                )
                color = GREEN if associate.relation_label == "固定队友" else CYAN
                draw.text((teammate_x + 14, top + 178), label, fill=color, font=self.fonts["tiny"])
                teammate_x += chip_width + 10

        if previous_engagements:
            history_top = top + (220 if associates else 160)
            draw.line((left + 26, history_top, right - 26, history_top), fill=GRID, width=1)
            draw.text(
                (left + 26, history_top + 14),
                "更早来犯",
                fill=MUTED,
                font=self.fonts["tiny"],
            )
            row_y = history_top + 42
            for previous in previous_engagements[:2]:
                draw.text(
                    (left + 26, row_y),
                    _aware(previous.last_seen).astimezone(SHANGHAI).strftime("%m-%d %H:%M"),
                    fill=MUTED,
                    font=self.fonts["tiny"],
                )
                draw.text(
                    (left + 190, row_y),
                    _fit_text(draw, previous.system_name, self.fonts["tiny"], 190),
                    fill=TEXT,
                    font=self.fonts["tiny"],
                )
                draw.text(
                    (left + 410, row_y),
                    f"{previous.fleet_size}人",
                    fill=CYAN,
                    font=self.fonts["tiny"],
                )
                draw.text(
                    (left + 500, row_y),
                    _fit_text(draw, previous.result_detail, self.fonts["tiny"], 400),
                    fill=TEXT,
                    font=self.fonts["tiny"],
                )
                draw.text(
                    (right - 220, row_y),
                    _engagement_value_text(previous) or "价值未知",
                    fill=YELLOW,
                    font=self.fonts["tiny"],
                    anchor="ra",
                )
                draw.text(
                    (right - 26, row_y),
                    f"{previous.composition_confidence.value}可信",
                    fill=_confidence_color(previous.composition_confidence),
                    font=self.fonts["tiny"],
                    anchor="ra",
                )
                row_y += 52

    def _role_bars(
        self,
        draw: ImageDraw.ImageDraw,
        bounds: tuple[int, int, int, int],
        metrics: list[CompositionMetric],
    ) -> None:
        left, top, right, _ = bounds
        x = left + 28
        y = top + 52
        bar_left = x + 128
        bar_right = right - 230
        maximum = max((metric.p75 for metric in metrics), default=1) or 1
        draw.text((right - 175, top + 18), "通常", fill=MUTED, font=self.fonts["tiny"])
        draw.text((right - 76, top + 18), "大场", fill=MUTED, font=self.fonts["tiny"])
        for metric in metrics[:5]:
            color = ROLE_COLORS.get(metric.name, CYAN)
            row_width = max(6, int((bar_right - bar_left) * metric.p75 / maximum))
            draw.text((x, y), metric.name, fill=TEXT, font=self.fonts["small"])
            draw.rounded_rectangle((bar_left, y + 4, bar_right, y + 24), 10, fill=GRID)
            draw.rounded_rectangle((bar_left, y + 4, bar_left + row_width, y + 24), 10, fill=color)
            draw.text(
                (right - 158, y),
                f"{metric.median:.0f}艘",
                fill=TEXT,
                font=self.fonts["small"],
                anchor="ra",
            )
            draw.text(
                (right - 48, y),
                f"{metric.p75:.0f}艘",
                fill=MUTED,
                font=self.fonts["small"],
                anchor="ra",
            )
            y += 40
        if not metrics:
            draw.text((x, top + 84), "暂无进攻舰队构成样本", fill=MUTED, font=self.fonts["body"])

    def _week_heatmap(
        self,
        draw: ImageDraw.ImageDraw,
        bounds: tuple[int, int, int, int],
        week_values: list[list[float]],
        hour_values: list[float],
    ) -> None:
        left, top, right, _ = bounds
        weekdays = "一二三四五六日"
        x = left + 76
        y = top + 46
        width = right - x - 28
        gap = 5
        cell_width = (width - gap * 23) / 24
        maximum = max((value for row in week_values for value in row), default=0) or 1
        for day in range(7):
            draw.text((left + 30, y + day * 34), weekdays[day], fill=MUTED, font=self.fonts["tiny"])
            for hour in range(24):
                value = week_values[day][hour]
                color = _blend((20, 43, 65), (255, 95, 109), value / maximum)
                cell_left = int(x + hour * (cell_width + gap))
                draw.rounded_rectangle(
                    (cell_left, y + day * 34, int(cell_left + cell_width), y + day * 34 + 24),
                    5,
                    fill=color,
                )
        label_y = y + 7 * 34 + 5
        for hour in range(0, 24, 3):
            label_x = int(x + hour * (cell_width + gap))
            draw.text((label_x, label_y), f"{hour:02d}", fill=MUTED, font=self.fonts["tiny"])

        bars_y = label_y + 42
        hour_max = max(hour_values, default=0) or 1
        bar_area_height = 78
        for hour, value in enumerate(hour_values):
            bar_left = int(x + hour * (cell_width + gap))
            bar_height = int(bar_area_height * value / hour_max)
            color = ORANGE if value / hour_max >= 0.7 else CYAN
            draw.rounded_rectangle(
                (
                    bar_left,
                    bars_y + bar_area_height - bar_height,
                    int(bar_left + cell_width),
                    bars_y + bar_area_height,
                ),
                4,
                fill=color,
            )
        draw.text((left + 30, bars_y + 18), "小时", fill=MUTED, font=self.fonts["tiny"])

    def _locations(
        self, draw: ImageDraw.ImageDraw, bounds: tuple[int, int, int, int], report: AnalysisReport
    ) -> None:
        left, top, right, _ = bounds
        draw.text((left + 26, top + 20), "活动区域", fill=MUTED, font=self.fonts["small"])
        y = top + 62
        draw.text((left + 26, y), "主要星域", fill=TEXT, font=self.fonts["h3"])
        y += 44
        region_total = sum(metric.value for metric in report.top_regions) or 1
        region_percentages = [
            NamedMetric(name=metric.name, value=metric.value / region_total * 100)
            for metric in report.top_regions[:3]
        ]
        y = self._compact_bars(
            draw,
            left + 26,
            y,
            right - 26,
            region_percentages,
            PURPLE,
            suffix="%",
        )
        y += 12
        draw.text((left + 26, y), "最近常见星系", fill=TEXT, font=self.fonts["h3"])
        y += 44
        self._compact_bars(draw, left + 26, y, right - 26, report.top_systems[:4], ORANGE)

    def _threat_components(
        self, draw: ImageDraw.ImageDraw, bounds: tuple[int, int, int, int], report: AnalysisReport
    ) -> None:
        left, top, right, _ = bounds
        color = _threat_color(report.threat_score)
        draw.text((left + 26, top + 20), "敌对威胁指数", fill=MUTED, font=self.fonts["small"])
        draw.text(
            (right - 26, top + 14),
            f"{report.threat_score} / 100  {report.threat_level}",
            fill=color,
            font=self.fonts["h3"],
            anchor="ra",
        )
        y = top + 66
        for component in report.threat_components[:6]:
            label_width = 116
            draw.text((left + 26, y), component.name, fill=TEXT, font=self.fonts["tiny"])
            bar_left = left + 26 + label_width
            bar_right = right - 82
            draw.rounded_rectangle((bar_left, y + 3, bar_right, y + 19), 7, fill=GRID)
            width = int((bar_right - bar_left) * component.score / component.maximum)
            if width:
                draw.rounded_rectangle((bar_left, y + 3, bar_left + width, y + 19), 7, fill=color)
            draw.text(
                (right - 26, y - 1),
                f"{component.score}/{component.maximum}",
                fill=MUTED,
                font=self.fonts["tiny"],
                anchor="ra",
            )
            draw.text(
                (bar_left, y + 25),
                _clip(component.explanation, 34),
                fill=MUTED,
                font=self.fonts["tiny"],
            )
            y += 58

    def _profile_cards(
        self,
        draw: ImageDraw.ImageDraw,
        margin: int,
        y: int,
        profiles: list[CharacterProfile],
    ) -> int:
        gap = 22
        card_width = (self.width - margin * 2 - gap) // 2
        card_height = 190
        for index, profile in enumerate(profiles):
            column = index % 2
            row = index // 2
            x = margin + column * (card_width + gap)
            top = y + row * (card_height + gap)
            draw.rounded_rectangle((x, top, x + card_width, top + card_height), 18, fill=PANEL)
            confidence_color = {"高": GREEN, "中": YELLOW, "低": RED}[profile.confidence.value]
            draw.text((x + 24, top + 18), _clip(profile.name, 26), fill=TEXT, font=self.fonts["h3"])
            draw.text(
                (x + card_width - 24, top + 21),
                f"置信度 {profile.confidence.value}",
                fill=confidence_color,
                font=self.fonts["small"],
                anchor="ra",
            )
            affiliation = profile.alliance_name or profile.corporation_name
            draw.text(
                (x + 24, top + 59), _clip(affiliation, 34), fill=MUTED, font=self.fonts["small"]
            )
            draw.text(
                (x + 24, top + 84), profile.candidate_label, fill=ORANGE, font=self.fonts["small"]
            )
            ships = " / ".join(metric.name for metric in profile.top_ships[:3]) or "未知"
            draw.text(
                (x + 24, top + 116),
                f"常用：{_clip(ships, 35)}",
                fill=TEXT,
                font=self.fonts["small"],
            )
            details = (
                f"活跃 {profile.peak_activity}  ·  击杀 {profile.kill_count} / 损失 {profile.loss_count}  ·  "
                f"同行交战 {profile.cooccurrence_score} 场"
            )
            draw.text((x + 24, top + 151), _clip(details, 48), fill=MUTED, font=self.fonts["tiny"])
        rows = (len(profiles) + 1) // 2
        return y + rows * (card_height + gap) + 18

    def _evidence(
        self, draw: ImageDraw.ImageDraw, margin: int, y: int, report: AnalysisReport
    ) -> None:
        reasons = report.threat_reasons[:3]
        gap = 14
        inner_left = margin + 24
        inner_right = self.width - margin - 24
        chip_width = (inner_right - inner_left - gap * 2) // 3
        for index, reason in enumerate(reasons):
            left = inner_left + index * (chip_width + gap)
            draw.rounded_rectangle((left, y + 18, left + chip_width, y + 68), 12, fill=PANEL_ALT)
            draw.ellipse((left + 16, y + 38, left + 24, y + 46), fill=ORANGE)
            draw.text(
                (left + 34, y + 29),
                _clip(reason, 21),
                fill=TEXT,
                font=self.fonts["tiny"],
            )

        warning = (
            report.warnings[0]
            if report.warnings
            else "zKillboard 为不完整的第三方公开样本，结论仅供情报参考"
        )
        draw.text(
            (inner_left, y + 94),
            f"数据提示 · {_clip(warning, 64)}",
            fill=YELLOW,
            font=self.fonts["tiny"],
        )

    def _compact_bars(
        self,
        draw: ImageDraw.ImageDraw,
        left: int,
        y: int,
        right: int,
        metrics: list[NamedMetric],
        color: str,
        suffix: str = "",
    ) -> int:
        maximum = max((metric.value for metric in metrics), default=1)
        for metric in metrics:
            draw.text((left, y), _clip(metric.name, 18), fill=TEXT, font=self.fonts["tiny"])
            bar_left = left + 205
            bar_right = right - 54
            draw.rounded_rectangle((bar_left, y + 3, bar_right, y + 18), 7, fill=GRID)
            width = int((bar_right - bar_left) * metric.value / maximum)
            draw.rounded_rectangle((bar_left, y + 3, bar_left + width, y + 18), 7, fill=color)
            draw.text(
                (right, y - 2),
                f"{metric.value:.0f}{suffix}",
                fill=MUTED,
                font=self.fonts["tiny"],
                anchor="ra",
            )
            y += 32
        if not metrics:
            draw.text((left, y), "暂无区域映射数据", fill=MUTED, font=self.fonts["small"])
            y += 34
        return y

    def _bars(
        self,
        draw: ImageDraw.ImageDraw,
        bounds: tuple[int, int, int, int],
        metrics: list[CompositionMetric],
        title: str,
        color: str,
        max_rows: int = 8,
    ) -> None:
        left, top, right, _ = bounds
        draw.text((left + 24, top + 18), title, fill=MUTED, font=self.fonts["small"])
        for index, metric in enumerate(metrics[:max_rows]):
            row_y = top + 58 + index * 64
            role_color = ROLE_COLORS.get(metric.role or "", color)
            draw.text(
                (left + 24, row_y),
                _fit_text(draw, metric.name, self.fonts["small"], 132),
                fill=TEXT,
                font=self.fonts["small"],
            )
            draw.text(
                (left + 172, row_y + 2),
                metric.role or "其他",
                fill=role_color,
                font=self.fonts["tiny"],
            )
            appeared = max(1, round(metric.occurrence_rate * metric.sample_count))
            draw.text(
                (right - 22, row_y - 1),
                f"{appeared}/{metric.sample_count} 场",
                fill=MUTED,
                font=self.fonts["tiny"],
                anchor="ra",
            )
            draw.text(
                (left + 24, row_y + 29),
                f"通常 {metric.median:.0f} 艘",
                fill=TEXT,
                font=self.fonts["tiny"],
            )
            draw.text(
                (left + 112, row_y + 29),
                f"大场 {metric.p75:.0f} 艘",
                fill=MUTED,
                font=self.fonts["tiny"],
            )
            bar_left = left + 218
            bar_right = right - 24
            width = int((bar_right - bar_left) * metric.occurrence_rate)
            draw.rounded_rectangle((bar_left, row_y + 34, bar_right, row_y + 45), 5, fill=GRID)
            if width:
                draw.rounded_rectangle(
                    (bar_left, row_y + 34, bar_left + width, row_y + 45),
                    5,
                    fill=role_color,
                )
        if not metrics:
            draw.text((left + 24, top + 82), "暂无数据", fill=MUTED, font=self.fonts["body"])

    def _section_title(self, draw: ImageDraw.ImageDraw, y: int, title: str) -> int:
        draw.text((64, y), title, fill=TEXT, font=self.fonts["h2"])
        return y + 50

    @staticmethod
    def _panel(draw: ImageDraw.ImageDraw, bounds: tuple[int, int, int, int]) -> None:
        draw.rounded_rectangle(bounds, 18, fill=PANEL)


def build_summary(report: AnalysisReport) -> str:
    doctrine = report.doctrines[0].name if report.doctrines else "未识别稳定体系"
    location = (
        report.top_regions[0].name
        if report.top_regions
        else report.top_systems[0].name
        if report.top_systems
        else "区域样本不足"
    )
    last = (
        report.last_activity.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M")
        if report.last_activity
        else "无"
    )
    invalid = f"；未识别 {len(report.invalid_names)} 人" if report.invalid_names else ""
    warning = f"\n提示：{report.warnings[0]}" if report.warnings else ""
    latest = report.latest_engagement
    isk_ratio = _format_isk_ratio(report.destroyed_value_30d, report.lost_value_30d)
    latest_line = (
        f"\n最近来犯：{latest.system_name}，{latest.outcome}，{latest.result_detail}；"
        f"观察到 {latest.fleet_size} 人编队"
        f"{f'，{_engagement_value_text(latest)}' if _engagement_value_text(latest) else ''}"
        if latest
        else ""
    )
    return (
        f"分析完成：有效 {report.resolved_count}/{report.requested_count} 人{invalid}，"
        f"覆盖 {report.coverage_ratio:.0%}\n"
        f"敌对威胁指数：{report.threat_score}/100（{report.threat_level}）\n"
        f"舰队体系：{doctrine}；规模：{report.fleet_size_label}\n"
        f"活跃：{report.peak_activity}；主要区域：{location}\n"
        f"近30天ISK战损 {isk_ratio}（摧毁 {_format_isk_short(report.destroyed_value_30d)} / "
        f"损失 {_format_isk_short(report.lost_value_30d)}）；"
        f"最后活动 {last}（北京时间）\n"
        f"样本 {report.data_events} 条 / {report.engagement_count} 场交战，"
        f"低置信度 {report.low_confidence_count} 人"
        f"{latest_line}"
        f"{warning}"
    )


def _find_font(configured: str | None) -> str:
    candidates = [
        configured,
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise RuntimeError("No usable font found; install fonts-noto-cjk or configure FONT_PATH")


def _relative_time(value: datetime | None, now: datetime) -> str:
    if value is None:
        return "无记录"
    seconds = max(0, int((_aware(now) - _aware(value)).total_seconds()))
    if seconds < 3600:
        return f"{max(1, seconds // 60)}分钟前"
    if seconds < 86400:
        return f"{seconds // 3600}小时前"
    return f"{seconds // 86400}天前"


def _engagement_time_label(value: datetime, now: datetime) -> str:
    local = _aware(value).astimezone(SHANGHAI).strftime("%m-%d %H:%M")
    return f"{local} · {_relative_time(value, now)}"


def _engagement_value_text(engagement: LatestEngagement) -> str:
    parts: list[str] = []
    if engagement.destroyed_value > 0:
        parts.append(f"摧毁 {_format_isk_short(engagement.destroyed_value)}")
    if engagement.lost_value > 0:
        parts.append(f"损失 {_format_isk_short(engagement.lost_value)}")
    return " · ".join(parts)


def _composition_status(engagement: LatestEngagement) -> str:
    confidence = f"{engagement.composition_confidence.value}可信"
    if engagement.composition_basis == "单条战报攻击方":
        observed = engagement.observed_attacker_count or engagement.fleet_size
        return f"{confidence} · 同战报 {observed} 人"
    if engagement.stable_pilot_count:
        temporary = (
            f" · 临时 {engagement.temporary_pilot_count} 人"
            if engagement.temporary_pilot_count
            else ""
        )
        return f"{confidence} · 稳定 {engagement.stable_pilot_count} 人{temporary}"
    return f"{confidence} · 无重复同行"


def _confidence_color(confidence: Confidence) -> str:
    if confidence == Confidence.HIGH:
        return GREEN
    if confidence == Confidence.MEDIUM:
        return YELLOW
    return ORANGE


def _format_isk(value: float) -> str:
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B ISK"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.0f}M ISK"
    if value >= 1_000:
        return f"{value / 1_000:.0f}K ISK"
    return f"{value:.0f} ISK"


def _format_isk_short(value: float) -> str:
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.0f}M"
    if value >= 1_000:
        return f"{value / 1_000:.0f}K"
    return f"{value:.0f}"


def _format_isk_ratio(destroyed: float, lost: float) -> str:
    if destroyed <= 0 and lost <= 0:
        return "无价值样本"
    if lost <= 0:
        return "暂无公开损失"
    ratio = destroyed / lost
    return f"{ratio:.1f} : 1"


def _isk_ratio_color(report: AnalysisReport) -> str:
    if report.destroyed_value_30d <= 0 and report.lost_value_30d <= 0:
        return MUTED
    if report.lost_value_30d <= 0:
        return GREEN
    efficiency = report.isk_efficiency_30d or 0
    if efficiency >= 0.6:
        return GREEN
    if efficiency >= 0.4:
        return YELLOW
    return RED


def _threat_color(score: int) -> str:
    if score >= 85:
        return PURPLE
    if score >= 70:
        return RED
    if score >= 50:
        return ORANGE
    if score >= 25:
        return YELLOW
    return GREEN


def _clip(value: str, length: int) -> str:
    return value if len(value) <= length else value[: length - 1] + "…"


def _fit_text(
    draw: ImageDraw.ImageDraw, value: str, font: ImageFont.FreeTypeFont, max_width: int
) -> str:
    if draw.textbbox((0, 0), value, font=font)[2] <= max_width:
        return value
    shortened = value
    while shortened and draw.textbbox((0, 0), shortened + "…", font=font)[2] > max_width:
        shortened = shortened[:-1]
    return shortened + "…" if shortened else "…"


def _blend(start: tuple[int, int, int], end: tuple[int, int, int], ratio: float) -> str:
    ratio = max(0.0, min(1.0, ratio))
    values = tuple(
        round(left + (right - left) * ratio) for left, right in zip(start, end, strict=True)
    )
    return f"#{values[0]:02x}{values[1]:02x}{values[2]:02x}"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)
