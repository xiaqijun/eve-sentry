from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from eve_risk.analysis import SHANGHAI
from eve_risk.domain import (
    AnalysisReport,
    AssociateCandidate,
    CompositionMetric,
    FleetSizeWindow,
    LatestEngagement,
    NamedMetric,
    PilotShipMetric,
)

DESIGN_WIDTH = 960

BG = "#090b12"
PANEL = "#151821"
PANEL_ALT = "#1b1e28"
CELL = "#1d2029"
BORDER = "#303544"
GRID = "#252a36"
TEXT = "#eef2f8"
MUTED = "#858da2"
DIM = "#596174"
CYAN = "#39c6ff"
BLUE = "#2878db"
GREEN = "#55e39b"
YELLOW = "#f5c96a"
ORANGE = "#f29b38"
RED = "#ff6376"
PURPLE = "#a980ff"


@dataclass(slots=True)
class ReportAssets:
    character_portraits: dict[int, bytes] = field(default_factory=dict)
    ship_icons: dict[int, bytes] = field(default_factory=dict)
    corporation_logos: dict[int, bytes] = field(default_factory=dict)
    alliance_logos: dict[int, bytes] = field(default_factory=dict)


class ReportRenderer:
    """Render a tall mobile-friendly EVE intelligence card for QQ."""

    def __init__(
        self,
        width: int = 960,
        max_height: int = 4096,
        font_path: str | None = None,
    ) -> None:
        self.width = max(720, width)
        self.max_height = max(1200, max_height)
        self.font_path = _find_font(font_path)
        self.fonts = {
            "hero": ImageFont.truetype(self.font_path, 42),
            "title": ImageFont.truetype(self.font_path, 31),
            "section": ImageFont.truetype(self.font_path, 27),
            "value": ImageFont.truetype(self.font_path, 26),
            "body": ImageFont.truetype(self.font_path, 22),
            "small": ImageFont.truetype(self.font_path, 18),
            "tiny": ImageFont.truetype(self.font_path, 15),
        }

    def render(self, report: AnalysisReport, assets: ReportAssets | None = None) -> bytes:
        assets = assets or ReportAssets()
        design_height = 2460
        image = Image.new("RGB", (DESIGN_WIDTH, design_height), BG)
        draw = ImageDraw.Draw(image)

        margin = 26
        gap = 22
        y = 24

        header = (margin, y, DESIGN_WIDTH - margin, y + 245)
        self._panel(draw, header)
        self._header(image, draw, header, report, assets)
        y = header[3] + gap

        stats = (margin, y, DESIGN_WIDTH - margin, y + 330)
        self._panel(draw, stats)
        self._stats(draw, stats, report)
        y = stats[3] + gap

        people_ships = (margin, y, DESIGN_WIDTH - margin, y + 445)
        self._panel(draw, people_ships)
        self._associates_and_ships(image, draw, people_ships, report, assets)
        y = people_ships[3] + gap

        activity = (margin, y, DESIGN_WIDTH - margin, y + 690)
        self._panel(draw, activity)
        self._activity(image, draw, activity, report, assets)
        y = activity[3] + gap

        regions = (margin, y, DESIGN_WIDTH - margin, y + 475)
        self._panel(draw, regions)
        self._regions(draw, regions, report.top_regions)
        y = regions[3] + 20

        footer = _footer_text(report)
        draw.text(
            (DESIGN_WIDTH // 2, y + 8),
            footer,
            fill=MUTED,
            font=self.fonts["small"],
            anchor="ma",
        )
        y += 52

        image = image.crop((0, 0, DESIGN_WIDTH, y))
        if self.width != DESIGN_WIDTH:
            ratio = self.width / DESIGN_WIDTH
            target_height = min(self.max_height, round(image.height * ratio))
            target_width = round(image.width * target_height / image.height)
            image = image.resize((target_width, target_height), Image.Resampling.LANCZOS)
        elif image.height > self.max_height:
            ratio = self.max_height / image.height
            image = image.resize(
                (round(image.width * ratio), self.max_height),
                Image.Resampling.LANCZOS,
            )

        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)
        return output.getvalue()

    def _header(
        self,
        image: Image.Image,
        draw: ImageDraw.ImageDraw,
        bounds: tuple[int, int, int, int],
        report: AnalysisReport,
        assets: ReportAssets,
    ) -> None:
        left, top, right, bottom = bounds
        profiles = report.profiles
        primary = profiles[0] if profiles else None
        single = report.resolved_count == 1 and primary is not None
        title = primary.name if single else f"敌对编队 · {report.resolved_count} 人"

        avatar = (left + 30, top + 26, left + 170, top + 166)
        initials = _initials(primary.name if primary else "EVE")
        portrait = assets.character_portraits.get(primary.character_id) if primary else None
        self._avatar(image, draw, avatar, initials, report.threat_score, portrait)

        tx = avatar[2] + 28
        draw.text(
            (tx, top + 22),
            _fit_text(draw, title, self.fonts["hero"], right - tx - 28),
            fill=TEXT,
            font=self.fonts["hero"],
        )

        badge_text = (
            f"{primary.character_id} [{primary.candidate_label}]"
            if single and primary
            else f"已解析 {report.resolved_count}/{report.requested_count}"
        )
        badge_width = max(
            150,
            draw.textbbox((0, 0), badge_text, font=self.fonts["tiny"])[2] + 28,
        )
        draw.rounded_rectangle((tx, top + 75, tx + badge_width, top + 107), 14, fill="#102a3c")
        draw.text(
            (tx + badge_width / 2, top + 91),
            badge_text,
            fill=CYAN,
            font=self.fonts["tiny"],
            anchor="mm",
        )

        if single and primary:
            affiliation_x = tx + 47
            alliance_logo = assets.alliance_logos.get(primary.alliance_id or 0)
            alliance_bounds = (tx, top + 116, tx + 37, top + 153)
            if not self._paste_asset(image, alliance_logo, alliance_bounds, radius=8):
                draw.rounded_rectangle(alliance_bounds, 8, fill="#253043")
                draw.text(
                    (tx + 18, top + 134),
                    "A",
                    fill=TEXT,
                    font=self.fonts["tiny"],
                    anchor="mm",
                )
            draw.rounded_rectangle(alliance_bounds, 8, outline=BORDER, width=1)
            draw.text(
                (affiliation_x, top + 120),
                _fit_text(
                    draw,
                    _affiliation_label(
                        primary.alliance_ticker or "",
                        primary.alliance_name or "无联盟",
                    ),
                    self.fonts["body"],
                    right - affiliation_x - 28,
                ),
                fill=MUTED if primary.alliance_name is None else TEXT,
                font=self.fonts["body"],
            )

            corporation_logo = assets.corporation_logos.get(primary.corporation_id or 0)
            corporation_bounds = (tx, top + 160, tx + 37, top + 197)
            if not self._paste_asset(image, corporation_logo, corporation_bounds, radius=8):
                draw.rounded_rectangle(corporation_bounds, 8, fill="#253043")
                draw.text(
                    (tx + 18, top + 178),
                    "C",
                    fill=TEXT,
                    font=self.fonts["tiny"],
                    anchor="mm",
                )
            draw.rounded_rectangle(corporation_bounds, 8, outline=BORDER, width=1)
            draw.text(
                (affiliation_x, top + 164),
                _fit_text(
                    draw,
                    _affiliation_label(
                        primary.corporation_ticker,
                        primary.corporation_name or "未知军团",
                    ),
                    self.fonts["body"],
                    right - affiliation_x - 28,
                ),
                fill=TEXT,
                font=self.fonts["body"],
            )
        elif profiles:
            affiliation = _top_affiliation(report.affiliations)
            draw.text(
                (tx, top + 124),
                _fit_text(draw, affiliation, self.fonts["body"], right - tx - 28),
                fill=TEXT,
                font=self.fonts["body"],
            )
            members = " · ".join(item.name for item in profiles[:4])
            if len(profiles) > 4:
                members += f" · +{len(profiles) - 4}"
            draw.text(
                (tx, top + 163),
                _fit_text(draw, members, self.fonts["small"], right - tx - 28),
                fill=MUTED,
                font=self.fonts["small"],
            )

        birthday = _birthday_text(primary.birthday if single and primary else None)
        draw.text(
            (left + 30, bottom - 38),
            f"出生日期：{birthday}",
            fill=MUTED,
            font=self.fonts["small"],
        )

        security = primary.security_status if single and primary else None
        security_text = f"{security:.2f}" if security is not None else "--"
        draw.text(
            (right - 30, bottom - 38),
            f"安全等级：{security_text}",
            fill=_security_color(security),
            font=self.fonts["body"],
            anchor="ra",
        )

    def _stats(
        self,
        draw: ImageDraw.ImageDraw,
        bounds: tuple[int, int, int, int],
        report: AnalysisReport,
    ) -> None:
        left, top, right, _ = bounds
        self._section_header(draw, left + 28, top + 20, right - 28, "击杀统计")
        lifetime = report.lifetime_stats
        kills = (
            lifetime.ships_destroyed
            if lifetime is not None
            else sum(profile.kill_count for profile in report.profiles)
        )
        losses = (
            lifetime.ships_lost
            if lifetime is not None
            else sum(profile.loss_count for profile in report.profiles)
        )
        points = (
            lifetime.points_destroyed
            if lifetime is not None
            else sum(profile.final_blow_count for profile in report.profiles)
        )
        destroyed_value = (
            lifetime.isk_destroyed
            if lifetime is not None
            else report.destroyed_value_30d
        )
        lost_value = (
            lifetime.isk_lost if lifetime is not None else report.lost_value_30d
        )
        ratio = (
            f"{points / kills:.4f}"
            if lifetime is not None and kills > 0
            else _format_ratio(destroyed_value, lost_value)
        )
        solo = str(lifetime.solo_kills) if lifetime is not None else (
            f"{(report.solo_ratio or 0) * 100:.0f}%"
            if report.solo_ratio is not None
            else "--"
        )

        cells = [
            ("击毁舰船", str(kills), GREEN),
            ("损失舰船", str(losses), RED),
            ("SOLO", solo, TEXT),
            ("击毁价值", _format_stat_isk(destroyed_value), TEXT),
            ("损失价值", _format_stat_isk(lost_value), TEXT),
            ("击杀点数", str(points), TEXT),
        ]
        inner_left = left + 26
        inner_right = right - 26
        cell_gap = 14
        cell_width = (inner_right - inner_left - cell_gap * 2) / 3
        for index, (label, value, color) in enumerate(cells):
            row = index // 3
            col = index % 3
            x1 = round(inner_left + col * (cell_width + cell_gap))
            y1 = top + 66 + row * 82
            x2 = round(x1 + cell_width)
            draw.rounded_rectangle((x1, y1, x2, y1 + 68), 10, fill=CELL)
            draw.text((x1 + 15, y1 + 10), label, fill=MUTED, font=self.fonts["tiny"])
            draw.text(
                (x1 + 15, y1 + 34),
                _fit_text(draw, value, self.fonts["body"], x2 - x1 - 28),
                fill=color,
                font=self.fonts["body"],
            )

        bar_y = top + 241
        third = (inner_right - inner_left - cell_gap * 2) / 3
        danger = lifetime.danger_ratio if lifetime is not None else report.threat_score
        gang = lifetime.gang_ratio if lifetime is not None else _team_score(report)
        ratio_progress = (
            min(1.0, (points / kills) / 5)
            if lifetime is not None and kills > 0
            else _ratio_score(destroyed_value, lost_value)
        )
        metrics = [
            ("船分比", ratio, ratio_progress, YELLOW),
            ("危险指数", f"{danger:.0f}%", danger / 100, RED),
            ("团队指数", f"{gang:.0f}%", gang / 100, CYAN),
        ]
        for index, (label, value, progress, color) in enumerate(metrics):
            x = round(inner_left + index * (third + cell_gap))
            width = round(third)
            draw.text((x, bar_y), label, fill=MUTED, font=self.fonts["tiny"])
            draw.text((x + width, bar_y), value, fill=TEXT, font=self.fonts["small"], anchor="ra")
            self._progress(draw, x, bar_y + 31, width, progress, color)

    def _associates_and_ships(
        self,
        image: Image.Image,
        draw: ImageDraw.ImageDraw,
        bounds: tuple[int, int, int, int],
        report: AnalysisReport,
        assets: ReportAssets,
    ) -> None:
        left, top, right, _ = bounds
        center = (left + right) // 2
        draw.line((center, top + 24, center, bounds[3] - 24), fill=BORDER, width=2)
        self._section_header(draw, left + 28, top + 20, center - 24, "小队规模")
        self._section_header(draw, center + 24, top + 20, right - 28, "常用舰船")
        self._fleet_size_rows(draw, left + 28, top + 72, center - 24, report.fleet_size_windows)
        if report.pilot_ships:
            self._pilot_ship_rows(
                image,
                draw,
                center + 24,
                top + 72,
                right - 28,
                report.pilot_ships[:5],
                assets,
            )
        else:
            self._ship_rows(
                image,
                draw,
                center + 24,
                top + 72,
                right - 28,
                report.top_ships[:5],
                assets,
            )

    def _fleet_size_rows(
        self,
        draw: ImageDraw.ImageDraw,
        left: int,
        top: int,
        right: int,
        windows: list[FleetSizeWindow],
    ) -> None:
        if not windows:
            draw.text((left, top + 20), "暂无小队规模样本", fill=MUTED, font=self.fonts["body"])
            return
        label_width = 132
        data_left = left + label_width
        column_width = (right - data_left) / 4
        bucket_labels = ("0–4人", "4–8人", "8–12人", "12人以上")
        for index, bucket_label in enumerate(bucket_labels):
            column_left = round(data_left + index * column_width)
            column_right = round(data_left + (index + 1) * column_width)
            draw.text(
                ((column_left + column_right) / 2, top - 27),
                bucket_label,
                fill=MUTED,
                font=self.fonts["tiny"],
                anchor="ma",
            )
        for row, window in enumerate(windows[:3]):
            y = top + row * 82
            draw.text(
                (left, y),
                _fit_text(draw, window.label, self.fonts["small"], label_width - 10),
                fill=TEXT,
                font=self.fonts["small"],
            )
            draw.text(
                (left, y + 25),
                f"{window.sample_count} KM",
                fill=DIM,
                font=self.fonts["tiny"],
            )
            maximum = max((bucket.count for bucket in window.buckets), default=1) or 1
            for index, bucket in enumerate(window.buckets[:4]):
                column_left = round(data_left + index * column_width)
                column_right = round(data_left + (index + 1) * column_width)
                draw.text(
                    ((column_left + column_right) / 2, y + 3),
                    str(bucket.count),
                    fill=TEXT,
                    font=self.fonts["body"],
                    anchor="ma",
                )
                self._progress(
                    draw,
                    column_left + 8,
                    y + 40,
                    max(1, column_right - column_left - 16),
                    bucket.count / maximum,
                    CYAN,
                    height=8,
                )

    def _associate_rows(
        self,
        image: Image.Image,
        draw: ImageDraw.ImageDraw,
        left: int,
        top: int,
        right: int,
        associates: list[AssociateCandidate],
        assets: ReportAssets,
    ) -> None:
        maximum = max(
            (item.score or float(item.engagement_count) for item in associates),
            default=1,
        )
        if not associates:
            draw.text((left, top + 20), "暂无稳定同行样本", fill=MUTED, font=self.fonts["body"])
            return
        for index, item in enumerate(associates):
            y = top + index * 69
            self._mini_avatar(
                image,
                draw,
                (left, y, left + 38, y + 38),
                _initials(item.name),
                assets.character_portraits.get(item.id),
            )
            draw.text(
                (left + 48, y + 1),
                _fit_text(draw, item.name, self.fonts["small"], right - left - 112),
                fill=TEXT,
                font=self.fonts["small"],
            )
            draw.text(
                (right, y + 1),
                f"{item.score:.1f}" if item.score else str(item.engagement_count),
                fill=TEXT,
                font=self.fonts["small"],
                anchor="ra",
            )
            self._progress(
                draw,
                left,
                y + 48,
                right - left,
                (item.score or item.engagement_count) / maximum,
                CYAN,
                height=10,
            )

    def _pilot_ship_rows(
        self,
        image: Image.Image,
        draw: ImageDraw.ImageDraw,
        left: int,
        top: int,
        right: int,
        ships: list[PilotShipMetric],
        assets: ReportAssets,
    ) -> None:
        maximum_kills = max((item.kill_count for item in ships), default=1) or 1
        maximum_losses = max((item.loss_count for item in ships), default=1) or 1
        draw.text(
            (right - 72, top - 31),
            "← 被击毁",
            fill=RED,
            font=self.fonts["tiny"],
            anchor="ra",
        )
        draw.text(
            (right, top - 31),
            "签名 →",
            fill=GREEN,
            font=self.fonts["tiny"],
            anchor="ra",
        )
        for index, ship in enumerate(ships):
            y = top + index * 69
            icon_bounds = (left, y, left + 38, y + 38)
            icon = assets.ship_icons.get(ship.id)
            if not self._paste_asset(image, icon, icon_bounds, radius=8):
                draw.rounded_rectangle(icon_bounds, 8, fill="#243043")
                draw.text(
                    (left + 19, y + 19),
                    str(index + 1),
                    fill=CYAN,
                    font=self.fonts["tiny"],
                    anchor="mm",
                )
            draw.rounded_rectangle(icon_bounds, 8, outline=BORDER, width=1)
            draw.text(
                (left + 48, y + 1),
                _fit_text(draw, ship.name, self.fonts["small"], right - left - 145),
                fill=TEXT,
                font=self.fonts["small"],
            )
            draw.text(
                (right - 72, y + 1),
                str(ship.loss_count),
                fill=RED,
                font=self.fonts["small"],
                anchor="ra",
            )
            draw.text(
                (right, y + 1),
                str(ship.kill_count),
                fill=GREEN,
                font=self.fonts["small"],
                anchor="ra",
            )
            bar_width = right - left - 48
            self._progress(
                draw,
                left + 48,
                y + 47,
                bar_width,
                ship.loss_count / maximum_losses,
                RED,
                height=9,
            )
            self._progress(
                draw,
                left + 48,
                y + 58,
                bar_width,
                ship.kill_count / maximum_kills,
                GREEN,
                height=9,
            )

    def _ship_rows(
        self,
        image: Image.Image,
        draw: ImageDraw.ImageDraw,
        left: int,
        top: int,
        right: int,
        ships: list[CompositionMetric],
        assets: ReportAssets,
    ) -> None:
        maximum = max((item.p75 for item in ships), default=1)
        if not ships:
            draw.text((left, top + 20), "暂无舰船样本", fill=MUTED, font=self.fonts["body"])
            return
        draw.text((right - 72, top - 31), "常见", fill=RED, font=self.fonts["tiny"], anchor="ra")
        draw.text((right, top - 31), "峰值", fill=GREEN, font=self.fonts["tiny"], anchor="ra")
        for index, ship in enumerate(ships):
            y = top + index * 69
            icon_bounds = (left, y, left + 38, y + 38)
            icon = assets.ship_icons.get(ship.id) if ship.id is not None else None
            if not self._paste_asset(image, icon, icon_bounds, radius=8):
                draw.rounded_rectangle(icon_bounds, 8, fill="#243043")
                draw.text(
                    (left + 19, y + 19),
                    str(index + 1),
                    fill=CYAN,
                    font=self.fonts["tiny"],
                    anchor="mm",
                )
            draw.rounded_rectangle(icon_bounds, 8, outline=BORDER, width=1)
            draw.text(
                (left + 48, y + 1),
                _fit_text(draw, ship.name, self.fonts["small"], right - left - 145),
                fill=TEXT,
                font=self.fonts["small"],
            )
            draw.text((right - 72, y + 1), f"{ship.median:.0f}", fill=RED, font=self.fonts["small"], anchor="ra")
            draw.text((right, y + 1), f"{ship.p75:.0f}", fill=GREEN, font=self.fonts["small"], anchor="ra")
            bar_width = right - left - 48
            self._progress(draw, left + 48, y + 47, bar_width, ship.median / maximum, RED, height=9)
            peak_width = max(4, round(bar_width * ship.p75 / maximum))
            draw.rounded_rectangle(
                (left + 48, y + 58, left + 48 + peak_width, y + 65),
                3,
                fill=GREEN,
            )

    def _activity(
        self,
        image: Image.Image,
        draw: ImageDraw.ImageDraw,
        bounds: tuple[int, int, int, int],
        report: AnalysisReport,
        assets: ReportAssets,
    ) -> None:
        left, top, right, _ = bounds
        self._section_header(draw, left + 28, top + 20, right - 28, "近期活动")
        draw.text((left + 28, top + 59), "活跃时间 · UTC", fill=TEXT, font=self.fonts["small"])

        heat_left = left + 96
        heat_right = right - 28
        heat_width = heat_right - heat_left
        heat_y = top + 92
        self._heat_strip(draw, heat_left, heat_y, heat_width, report.activity_hours, ORANGE)
        for hour in (0, 6, 12, 18, 24):
            x = heat_left + round(heat_width * hour / 24)
            draw.text((x, heat_y + 34), f"{hour:02d}", fill=DIM, font=self.fonts["tiny"], anchor="ma")
        draw.text((left + 28, heat_y + 8), "24H", fill=MUTED, font=self.fonts["tiny"])

        weekday_values = [sum(row) for row in report.activity_week_hours]
        week_y = heat_y + 65
        self._heat_strip(draw, heat_left, week_y, heat_width, weekday_values, GREEN)
        for index, label in enumerate("一二三四五六日"):
            x = heat_left + round(heat_width * (index + 0.5) / 7)
            draw.text((x, week_y + 34), label, fill=DIM, font=self.fonts["tiny"], anchor="ma")
        draw.text((left + 28, week_y + 8), "7D", fill=MUTED, font=self.fonts["tiny"])

        timeline_y = week_y + 72
        self._timeline(
            draw,
            heat_left,
            timeline_y,
            heat_width,
            report.recent_engagements,
            report.generated_at,
            report.data_window_days,
        )
        timeline_label = "历史" if report.data_window_days > 90 else f"{report.data_window_days}D"
        draw.text(
            (left + 28, timeline_y + 8),
            timeline_label,
            fill=MUTED,
            font=self.fonts["tiny"],
        )

        table_y = timeline_y + 68
        draw.text((left + 28, table_y), "最近战报", fill=TEXT, font=self.fonts["body"])
        draw.text((left + 28, table_y + 34), "时间", fill=MUTED, font=self.fonts["tiny"])
        draw.text((left + 160, table_y + 34), "星系", fill=MUTED, font=self.fonts["tiny"])
        draw.text((right - 28, table_y + 34), "战损", fill=MUTED, font=self.fonts["tiny"], anchor="ra")
        rows = report.recent_engagements[:5]
        if not rows and report.latest_engagement:
            rows = [report.latest_engagement]
        if not rows:
            draw.text((left + 28, table_y + 88), "暂无近期交战记录", fill=MUTED, font=self.fonts["body"])
            return
        for index, engagement in enumerate(rows):
            row_y = table_y + 63 + index * 61
            draw.rounded_rectangle((left + 24, row_y, right - 24, row_y + 51), 8, fill=CELL)
            observed_at = engagement.last_seen.astimezone(UTC).strftime("%m-%d %H:%M")
            draw.text((left + 36, row_y + 15), observed_at, fill=MUTED, font=self.fonts["tiny"])
            location = engagement.system_name
            draw.text(
                (left + 168, row_y + 13),
                _fit_text(draw, location, self.fonts["small"], 300),
                fill=TEXT,
                font=self.fonts["small"],
            )
            self._battle_ship_icons(
                image,
                draw,
                right - 330,
                row_y + 11,
                engagement,
                assets,
            )
            value = _battle_value_text(engagement)
            draw.text(
                (right - 36, row_y + 15),
                value,
                fill=_outcome_color(engagement),
                font=self.fonts["tiny"],
                anchor="ra",
            )

    def _battle_ship_icons(
        self,
        image: Image.Image,
        draw: ImageDraw.ImageDraw,
        left: int,
        top: int,
        engagement: LatestEngagement,
        assets: ReportAssets,
    ) -> None:
        x = left
        for items, color in (
            (engagement.lost_ships[:2], RED),
            (engagement.destroyed_ships[:2], GREEN),
        ):
            if x > left:
                draw.text(
                    (x + 3, top + 13),
                    "/",
                    fill=MUTED,
                    font=self.fonts["tiny"],
                    anchor="lm",
                )
                x += 14
            if not items:
                draw.text(
                    (x + 8, top + 13),
                    "—",
                    fill=DIM,
                    font=self.fonts["tiny"],
                    anchor="mm",
                )
                x += 20
                continue
            for item in items:
                bounds = (x, top, x + 26, top + 26)
                icon = assets.ship_icons.get(item.id) if item.id is not None else None
                if not self._paste_asset(image, icon, bounds, radius=6):
                    draw.rounded_rectangle(bounds, 6, fill="#243043")
                draw.rounded_rectangle(bounds, 6, outline=color, width=1)
                if item.count > 1:
                    draw.text(
                        (x + 25, top + 25),
                        str(item.count),
                        fill=TEXT,
                        stroke_width=2,
                        stroke_fill=BG,
                        font=self.fonts["tiny"],
                        anchor="rs",
                    )
                x += 30

    def _regions(
        self,
        draw: ImageDraw.ImageDraw,
        bounds: tuple[int, int, int, int],
        regions: list[NamedMetric],
    ) -> None:
        left, top, right, bottom = bounds
        self._section_header(draw, left + 28, top + 20, right - 28, "活跃星域")
        area = (left + 28, top + 70, right - 28, bottom - 28)
        if not regions:
            draw.rounded_rectangle(area, 10, fill=CELL)
            draw.text(
                ((area[0] + area[2]) // 2, (area[1] + area[3]) // 2),
                "暂无星域映射数据",
                fill=MUTED,
                font=self.fonts["body"],
                anchor="mm",
            )
            return
        self._treemap(draw, area, regions[:8])

    def _treemap(
        self,
        draw: ImageDraw.ImageDraw,
        bounds: tuple[int, int, int, int],
        metrics: list[NamedMetric],
    ) -> None:
        total = sum(max(0, item.value) for item in metrics) or 1
        first = metrics[0]
        first_ratio = max(0.42, min(0.62, first.value / total))
        left, top, right, bottom = bounds
        split = round(left + (right - left) * first_ratio)
        self._region_box(draw, (left, top, split - 3, bottom), first, 0)
        remaining = metrics[1:]
        if not remaining:
            return
        remaining_total = sum(max(0, item.value) for item in remaining) or 1
        y = top
        cumulative = 0.0
        for index, metric in enumerate(remaining):
            if index == len(remaining) - 1:
                next_y = bottom
            else:
                cumulative += max(0, metric.value)
                next_y = round(top + (bottom - top) * cumulative / remaining_total)
            self._region_box(draw, (split + 3, y, right, max(y, next_y - 3)), metric, index + 1)
            y = next_y

    def _region_box(
        self,
        draw: ImageDraw.ImageDraw,
        bounds: tuple[int, int, int, int],
        metric: NamedMetric,
        index: int,
    ) -> None:
        colors = ["#38bdf2", "#2862bd", "#2849a2", "#23418e", "#1f397d", "#1b336f", "#182e63", "#152957"]
        color = colors[min(index, len(colors) - 1)]
        draw.rectangle(bounds, fill=color, outline=BG, width=2)
        width = bounds[2] - bounds[0]
        height = bounds[3] - bounds[1]
        if width < 90 or height < 38:
            return
        font = self.fonts["title"] if width > 240 and height > 130 else self.fonts["small"]
        text = f"{metric.name}\n{metric.value:.0f}"
        draw.multiline_text(
            ((bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2),
            text,
            fill=TEXT,
            font=font,
            anchor="mm",
            align="center",
            spacing=6,
        )

    def _timeline(
        self,
        draw: ImageDraw.ImageDraw,
        left: int,
        top: int,
        width: int,
        engagements: list[LatestEngagement],
        generated_at: datetime,
        window_days: int,
    ) -> None:
        draw.rounded_rectangle((left, top, left + width, top + 27), 7, fill="#071012")
        window = timedelta(days=max(1, window_days))
        cutoff = _aware(generated_at) - window
        for engagement in engagements[:30]:
            value = _aware(engagement.last_seen)
            ratio = (value - cutoff).total_seconds() / window.total_seconds()
            if ratio < 0 or ratio > 1:
                continue
            x = left + round(width * ratio)
            draw.rectangle((x - 3, top, x + 3, top + 27), fill=CYAN)
        draw.text(
            (left, top + 36),
            cutoff.astimezone(UTC).strftime("%m-%d"),
            fill=DIM,
            font=self.fonts["tiny"],
        )
        draw.text(
            (left + width, top + 36),
            generated_at.astimezone(UTC).strftime("%m-%d"),
            fill=DIM,
            font=self.fonts["tiny"],
            anchor="ra",
        )

    def _heat_strip(
        self,
        draw: ImageDraw.ImageDraw,
        left: int,
        top: int,
        width: int,
        values: list[float],
        accent: str,
    ) -> None:
        count = max(1, len(values))
        gap = 2
        cell_width = (width - gap * (count - 1)) / count
        maximum = max(values, default=0) or 1
        start = _hex_to_rgb("#11151c")
        end = _hex_to_rgb(accent)
        for index, value in enumerate(values or [0]):
            x1 = round(left + index * (cell_width + gap))
            x2 = round(x1 + cell_width)
            color = _blend(start, end, value / maximum)
            draw.rectangle((x1, top, x2, top + 27), fill=color)

    def _avatar(
        self,
        image: Image.Image,
        draw: ImageDraw.ImageDraw,
        bounds: tuple[int, int, int, int],
        initials: str,
        threat_score: int,
        portrait: bytes | None,
    ) -> None:
        del threat_score
        draw.rounded_rectangle(bounds, 16, fill="#111722", outline=BORDER, width=2)
        inner = (bounds[0] + 2, bounds[1] + 2, bounds[2] - 2, bounds[3] - 2)
        if not self._paste_asset(image, portrait, inner, radius=14):
            draw.rounded_rectangle(inner, 14, fill="#1a2535")
            draw.text(
                ((inner[0] + inner[2]) // 2, (inner[1] + inner[3]) // 2),
                initials,
                fill=TEXT,
                font=self.fonts["title"],
                anchor="mm",
            )
        draw.rounded_rectangle(inner, 14, outline=BORDER, width=1)

    def _mini_avatar(
        self,
        image: Image.Image,
        draw: ImageDraw.ImageDraw,
        bounds: tuple[int, int, int, int],
        initials: str,
        portrait: bytes | None,
    ) -> None:
        radius = (bounds[2] - bounds[0]) // 2
        if not self._paste_asset(image, portrait, bounds, radius=radius):
            draw.ellipse(bounds, fill="#263246")
            draw.text(
                ((bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2),
                initials,
                fill=TEXT,
                font=self.fonts["tiny"],
                anchor="mm",
            )
        draw.ellipse(bounds, outline=BORDER, width=1)

    @staticmethod
    def _paste_asset(
        image: Image.Image,
        data: bytes | None,
        bounds: tuple[int, int, int, int],
        *,
        radius: int,
    ) -> bool:
        if not data:
            return False
        width = bounds[2] - bounds[0]
        height = bounds[3] - bounds[1]
        if width <= 0 or height <= 0:
            return False
        try:
            source = Image.open(io.BytesIO(data)).convert("RGB")
            fitted = ImageOps.fit(source, (width, height), method=Image.Resampling.LANCZOS)
        except Exception:
            return False
        mask = Image.new("L", (width, height), 0)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.rounded_rectangle((0, 0, width, height), radius, fill=255)
        image.paste(fitted, (bounds[0], bounds[1]), mask)
        return True

    def _section_header(
        self,
        draw: ImageDraw.ImageDraw,
        left: int,
        top: int,
        right: int,
        title: str,
    ) -> None:
        draw.text((left, top), title, fill=CYAN, font=self.fonts["section"])
        title_width = draw.textbbox((0, 0), title, font=self.fonts["section"])[2]
        draw.line((left + title_width + 16, top + 18, right, top + 18), fill=BORDER, width=2)

    @staticmethod
    def _panel(draw: ImageDraw.ImageDraw, bounds: tuple[int, int, int, int]) -> None:
        draw.rounded_rectangle(bounds, 20, fill=PANEL, outline=BORDER, width=2)

    @staticmethod
    def _progress(
        draw: ImageDraw.ImageDraw,
        left: int,
        top: int,
        width: int,
        value: float,
        color: str,
        height: int = 12,
    ) -> None:
        value = max(0.0, min(1.0, value))
        draw.rounded_rectangle((left, top, left + width, top + height), height // 2, fill=GRID)
        filled = round(width * value)
        if filled:
            draw.rounded_rectangle((left, top, left + filled, top + height), height // 2, fill=color)


def build_summary(report: AnalysisReport) -> str:
    location = (
        report.top_regions[0].name
        if report.top_regions
        else report.top_systems[0].name
        if report.top_systems
        else "样本不足"
    )
    last = (
        report.last_activity.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M")
        if report.last_activity
        else "无记录"
    )
    invalid = f"；未识别 {len(report.invalid_names)} 人" if report.invalid_names else ""
    warning = f"\n提示：{report.warnings[0]}" if report.warnings else ""
    return (
        f"分析完成：有效 {report.resolved_count}/{report.requested_count} 人{invalid}，"
        f"覆盖 {report.coverage_ratio:.0%}\n"
        f"敌对威胁指数：{report.threat_score}/100（{report.threat_level}）\n"
        f"活跃：{report.peak_activity}；主要区域：{location}\n"
        f"近30天战损：击毁 {_format_isk(report.destroyed_value_30d)} / "
        f"损失 {_format_isk(report.lost_value_30d)}\n"
        f"公开样本 {report.data_events} 条 / {report.engagement_count} 场交战；"
        f"最后活动 {last}（北京时间）"
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


def _top_affiliation(affiliations: list[NamedMetric]) -> str:
    if not affiliations:
        return "未知军团 / 联盟"
    return " · ".join(item.name for item in affiliations[:2])


def _affiliation_label(ticker: str, name: str) -> str:
    normalized_ticker = str(ticker or "").strip()
    normalized_name = str(name or "").strip()
    if normalized_ticker:
        return f"[{normalized_ticker}] {normalized_name}"
    return normalized_name


def _birthday_text(value: datetime | None) -> str:
    if value is None:
        return "--"
    return value.strftime("%Y-%m-%d")


def _security_color(value: float | None) -> str:
    if value is None:
        return MUTED
    if value < 0:
        return RED
    return GREEN


def _initials(value: str) -> str:
    words = [item for item in value.replace("·", " ").split() if item]
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    compact = "".join(char for char in value if char.isalnum())
    return (compact[:2] or "EV").upper()


def _threat_level(score: int) -> float:
    return max(0.0, min(5.0, score / 20))


def _team_score(report: AnalysisReport) -> float:
    if report.solo_ratio is None:
        return min(100.0, report.engagement_count * 8.0)
    return max(0.0, min(100.0, (1 - report.solo_ratio) * 100))


def _ratio_score(destroyed: float, lost: float) -> float:
    if destroyed <= 0 and lost <= 0:
        return 0
    if lost <= 0:
        return 1
    return min(1.0, destroyed / lost / 5)


def _format_ratio(destroyed: float, lost: float) -> str:
    if destroyed <= 0 and lost <= 0:
        return "--"
    if lost <= 0:
        return "∞"
    return f"{destroyed / lost:.2f}"


def _format_isk(value: float) -> str:
    if value >= 1_000_000_000_000:
        return f"{value / 1_000_000_000_000:.2f} T ISK"
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f} B ISK"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f} M ISK"
    if value >= 1_000:
        return f"{value / 1_000:.0f} K ISK"
    return f"{value:.0f} ISK"


def _format_stat_isk(value: float) -> str:
    if value >= 1_000_000_000_000:
        return f"{value / 1_000_000_000_000:.3f} T ISK"
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.3f} B ISK"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.3f} M ISK"
    if value >= 1_000:
        return f"{value / 1_000:.3f} K ISK"
    return f"{value:.0f} ISK"


def _engagement_value(engagement: LatestEngagement) -> str:
    if engagement.destroyed_value or engagement.lost_value:
        return f"{_format_isk(engagement.destroyed_value)} / {_format_isk(engagement.lost_value)}"
    return engagement.outcome


def _battle_value_text(engagement: LatestEngagement) -> str:
    return (
        f"{_format_battle_isk(engagement.lost_value)} / "
        f"{_format_battle_isk(engagement.destroyed_value)}"
    )


def _format_battle_isk(value: float) -> str:
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.3f} B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.3f} M"
    if value >= 1_000:
        return f"{value / 1_000:.3f} K"
    return f"{value:.3f}"


def _outcome_color(engagement: LatestEngagement) -> str:
    if engagement.lost_value > engagement.destroyed_value:
        return RED
    if engagement.destroyed_value > 0:
        return GREEN
    return CYAN


def _threat_color(score: int) -> str:
    if score >= 80:
        return PURPLE
    if score >= 60:
        return RED
    if score >= 40:
        return ORANGE
    if score >= 20:
        return YELLOW
    return GREEN


def _footer_text(report: AnalysisReport) -> str:
    window_label = "历史样本" if report.data_window_days > 90 else f"{report.data_window_days}D"
    return (
        f"EVE RISK · Tranquility  ·  数据窗口 {window_label}  ·  公开战报 {report.data_events} 条"
    )


def _fit_text(
    draw: ImageDraw.ImageDraw,
    value: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> str:
    if draw.textbbox((0, 0), value, font=font)[2] <= max_width:
        return value
    shortened = value
    while shortened and draw.textbbox((0, 0), shortened + "…", font=font)[2] > max_width:
        shortened = shortened[:-1]
    return shortened + "…" if shortened else "…"


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[index : index + 2], 16) for index in (0, 2, 4))


def _blend(
    start: tuple[int, int, int],
    end: tuple[int, int, int],
    ratio: float,
) -> str:
    ratio = max(0.0, min(1.0, ratio))
    values = tuple(
        round(left + (right - left) * ratio)
        for left, right in zip(start, end, strict=True)
    )
    return f"#{values[0]:02x}{values[1]:02x}{values[2]:02x}"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)
