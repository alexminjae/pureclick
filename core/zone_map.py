"""Project onestop seat coordinates into a pickable venue sketch.

Selection is a *watch rectangle* in seatMeta space (posLeft/posTop). The hunt
still polls by blockKey, but only seats inside the rectangle are taken — so a
drag on the copied grape map means “취켓 this area”, not a whole NOL section.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence


DRAG_CLICK_PX = 6
CLICK_RADIUS_PX = 14
# Blocks whose seats sit farther than this gap from the main house are kept out
# of the framing bounds so the sketch matches the official zoomed house view.
CLUSTER_GAP = 18.0


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def parse_box(block: dict[str, Any]) -> tuple[float, float, float, float] | None:
    left = _num(block.get("left", block.get("absoluteLeft")))
    top = _num(block.get("top", block.get("absoluteTop")))
    right = _num(block.get("right", block.get("absoluteRight")))
    bottom = _num(block.get("bottom", block.get("absoluteBottom")))
    if None in (left, top, right, bottom):
        return None
    if right <= left or bottom <= top:
        return None
    return (left, top, right, bottom)


def normalize_rect(rect: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    left, top, right, bottom = rect
    if right < left:
        left, right = right, left
    if bottom < top:
        top, bottom = bottom, top
    return (left, top, right, bottom)


def parse_watch_rect(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, dict):
        return None
    left = _num(value.get("left"))
    top = _num(value.get("top"))
    right = _num(value.get("right"))
    bottom = _num(value.get("bottom"))
    if None in (left, top, right, bottom):
        return None
    rect = normalize_rect((left, top, right, bottom))
    if rect[2] - rect[0] < 0.5 or rect[3] - rect[1] < 0.5:
        return None
    return rect


def point_in_rect(x: float, y: float, rect: tuple[float, float, float, float]) -> bool:
    left, top, right, bottom = rect
    return left <= x <= right and top <= y <= bottom


def boxes_intersect(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> bool:
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


@dataclass(frozen=True)
class PlacedBlock:
    key: str
    name: str
    box: tuple[float, float, float, float]

    @property
    def area(self) -> float:
        left, top, right, bottom = self.box
        return max(0.0, right - left) * max(0.0, bottom - top)


@dataclass(frozen=True)
class PlacedSeat:
    key: str
    x: float
    y: float
    venue_x: float
    venue_y: float
    # False when seatMeta reports isExposable: false for it — a real seat in a
    # block this round is not selling. Drawn, so the picker shows the same room
    # the 예매 창 does, but never watched: no cancellation can appear in a seat
    # nobody can buy.
    sellable: bool = True


@dataclass(frozen=True)
class VenueView:
    seats: tuple[PlacedSeat, ...]
    blocks: tuple[PlacedBlock, ...]
    stage: tuple[float, float, float, float] | None
    scale: float
    # Venue-space origin of the projected cloud (before scale/offset).
    origin_left: float
    origin_top: float
    offset_x: float
    offset_y: float

    def canvas_to_venue(self, x: float, y: float) -> tuple[float, float]:
        if self.scale <= 0:
            return (x, y)
        return (
            self.origin_left + (x - self.offset_x) / self.scale,
            self.origin_top + (y - self.offset_y) / self.scale,
        )

    def venue_to_canvas(self, x: float, y: float) -> tuple[float, float]:
        return (
            self.offset_x + (x - self.origin_left) * self.scale,
            self.offset_y + (y - self.origin_top) * self.scale,
        )

    def canvas_rect_to_venue(self, rect: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
        left, top, right, bottom = normalize_rect(rect)
        x0, y0 = self.canvas_to_venue(left, top)
        x1, y1 = self.canvas_to_venue(right, bottom)
        return normalize_rect((x0, y0, x1, y1))


def primary_frame_seats(seats: Sequence[tuple]) -> list[tuple]:
    """Keep the main house for framing — drop far-away side blocks like 002."""
    if len(seats) < 8:
        return list(seats)
    by_key: dict[str, list[tuple]] = {}
    for item in seats:
        by_key.setdefault(item[0], []).append(item)
    if len(by_key) <= 1:
        return list(seats)

    # Largest block is the house the official map zooms to.
    primary_key = max(by_key.items(), key=lambda item: len(item[1]))[0]
    prim = by_key[primary_key]
    # Indexed, not unpacked: a point carries a sellability flag as well now, and
    # this function has no interest in it.
    left = min(point[1] for point in prim)
    top = min(point[2] for point in prim)
    right = max(point[1] for point in prim)
    bottom = max(point[2] for point in prim)

    # The gap has to scale with the venue, not be a fixed number of units.
    # Seat coordinates are in each show's own space with no common scale: on
    # 26005128 the 1층 block spans posTop 68-131 while 2층 sits at 153-189, so a
    # flat 18 dropped the entire second floor for being 4 units too far. Measured
    # across the saved maps, a fixed gap drew 2 blocks of 17, 1 of 5, and 1 of 3
    # — and a floor that is not drawn cannot be selected.
    gap = max(CLUSTER_GAP, (right - left) * 0.6, (bottom - top) * 0.6)
    kept = list(prim)
    for key, pts in by_key.items():
        if key == primary_key:
            continue
        cx = sum(point[1] for point in pts) / len(pts)
        cy = sum(point[2] for point in pts) / len(pts)
        # Outside the house with a clear gap → hide from the framed copy.
        if cx < left - gap or cx > right + gap or cy < top - gap or cy > bottom + gap:
            continue
        kept.extend(pts)
    return kept


def house_frame(points: Sequence[tuple]) -> list[tuple]:
    """Bounds for a selection map: everything sellable, plus any dark block
    sitting against it.

    `include_all` exists because framing on the main house alone dropped whole
    floors — 3 blocks of 17 on one venue — and a seat that is not drawn cannot
    be dragged over. But framing on *literally* everything is no better: once
    the sketch started carrying blocks that are not on sale, a 90-seat side
    island 50 units clear of the house stretched the frame by a third and
    squeezed the real room into three quarters of the canvas.

    So the rule is about what you can buy, not about size: a block with sellable
    seats always frames. A block with none frames only if it touches the house.
    """
    live = [point for point in points if point[3]]
    if not live:
        return list(points)

    left = min(point[1] for point in live)
    top = min(point[2] for point in live)
    right = max(point[1] for point in live)
    bottom = max(point[2] for point in live)
    # Same scaling as primary_frame_seats: seat coordinates have no common
    # scale between venues, so a fixed gap drops a second floor on one map and
    # keeps a car park on the next.
    gap = max(CLUSTER_GAP, (right - left) * 0.6, (bottom - top) * 0.6)

    dark: dict[str, list[tuple]] = {}
    for point in points:
        if not point[3]:
            dark.setdefault(point[0], []).append(point)

    kept = list(live)
    for pts in dark.values():
        # Nearest edge, not the centroid. A block's centre is half its width
        # away from the house it is touching, so a centroid test called the
        # blocks immediately beside the sellable ones distant and clipped them:
        # on 26012673 the D column starts 5 units past the last sellable seat
        # and its centre sits 55 further still.
        if (
            min(point[1] for point in pts) <= right + gap
            and max(point[1] for point in pts) >= left - gap
            and min(point[2] for point in pts) <= bottom + gap
            and max(point[2] for point in pts) >= top - gap
        ):
            kept.extend(pts)
    return kept


def _fit(
    left: float,
    top: float,
    right: float,
    bottom: float,
    width: float,
    height: float,
    padding: float,
) -> tuple[float, float, float]:
    span_x = max(right - left, 1.0)
    span_y = max(bottom - top, 1.0)
    top_pad = padding + 28
    inner_w = width - padding * 2
    inner_h = height - top_pad - padding
    if inner_w <= 1 or inner_h <= 1:
        return 1.0, padding, padding
    scale = min(inner_w / span_x, inner_h / span_y)
    offset_x = padding + (inner_w - span_x * scale) / 2
    offset_y = top_pad + (inner_h - span_y * scale) / 2
    return scale, offset_x, offset_y


def project_blocks(
    blocks: Sequence[dict[str, Any]],
    width: float,
    height: float,
    padding: float = 18.0,
) -> list[PlacedBlock]:
    sources: list[tuple[str, str, tuple[float, float, float, float]]] = []
    for block in blocks:
        box = parse_box(block)
        if not box:
            continue
        key = str(block.get("key") or block.get("block_key") or "").strip()
        if not key:
            continue
        name = str(block.get("name") or block.get("label") or block.get("block_name") or key)
        sources.append((key, name, box))
    if not sources or width <= padding * 2 or height <= padding * 2:
        return []

    left = min(box[0] for _, _, box in sources)
    top = min(box[1] for _, _, box in sources)
    right = max(box[2] for _, _, box in sources)
    bottom = max(box[3] for _, _, box in sources)
    scale, offset_x, offset_y = _fit(left, top, right, bottom, width, height, padding)
    return [
        PlacedBlock(
            key=key,
            name=name,
            box=(
                offset_x + (box[0] - left) * scale,
                offset_y + (box[1] - top) * scale,
                offset_x + (box[2] - left) * scale,
                offset_y + (box[3] - top) * scale,
            ),
        )
        for key, name, box in sources
    ]


def seat_pitch(seats: Sequence[Any]) -> float:
    """Canvas distance between neighbouring seats, for drawing them at size.

    The picker drew every seat as a fixed 1.7px dot whatever the venue, so a
    tight house came out as a smear and a small one as scattered specks. NOL
    sizes its circles to the row and column pitch, and matching that is what
    makes the copy read as the same room.

    Measured one block at a time. Pooling the whole venue looked simpler and was
    badly wrong: blocks sit at fractional offsets from each other, so the
    combined list of distinct positions is full of near-zero gaps that have
    nothing to do with seat spacing. On a 1913-seat venue whose every block has
    a 13.95px pitch, the pooled median came out at 0.24px and every seat was
    drawn at the minimum size.

    Measured on the drawn points rather than the raw venue, so a downsampled
    sketch gets circles matching the spacing actually on screen.
    """
    if not seats:
        return 3.0

    def median_gap(values: list[float]) -> float:
        # Distinct positions only: every seat in a column shares an x, and the
        # zero gaps between them would drag any average to nothing.
        uniq = sorted({round(v, 2) for v in values})
        gaps = sorted(b - a for a, b in zip(uniq, uniq[1:]) if b - a > 0.01)
        return gaps[len(gaps) // 2] if gaps else 0.0

    by_block: dict[str, list[Any]] = {}
    for seat in seats:
        by_block.setdefault(seat.key, []).append(seat)

    pitches: list[float] = []
    for block in by_block.values():
        # A block one seat wide has no spacing to report; the others speak.
        gaps = [
            gap
            for gap in (median_gap([s.x for s in block]), median_gap([s.y for s in block]))
            if gap > 0
        ]
        if gaps:
            pitches.append(min(gaps))

    if not pitches:
        return 3.0
    pitches.sort()
    return pitches[len(pitches) // 2]


# Rows within this fraction of the venue depth count as "the front", and are
# what the drawn stage spans. Wide enough to survive a venue whose first row is
# a short VIP strip, narrow enough not to creep back to the whole house.
STAGE_FRONT_BAND = 0.18
STAGE_MIN_WIDTH = 80.0


def project_venue(
    blocks: Sequence[dict[str, Any]],
    seats: Sequence[dict[str, Any]],
    width: float,
    height: float,
    padding: float = 18.0,
    include_all: bool = False,
) -> VenueView:
    points: list[tuple[str, float, float, bool]] = []
    for seat in seats or []:
        key = str(seat.get("k") or seat.get("key") or seat.get("block_key") or "").strip()
        x = _num(seat.get("x", seat.get("posLeft")))
        y = _num(seat.get("y", seat.get("posTop")))
        if not key or x is None or y is None:
            continue
        points.append((key, x, y, is_sellable(seat)))

    # Framing normally follows the main house so one far island cannot shrink
    # it. `include_all` overrides that for a *selection* surface, where a seat
    # that is not drawn cannot be dragged over: measured on the saved maps,
    # framing-only drawing reached 3 blocks of 17 on 26011315 and 1 of 3 on
    # 26005128, silently putting most of the venue out of reach of 취켓팅.
    frame = house_frame(points) if include_all else (primary_frame_seats(points) if points else [])
    if frame and width > padding * 2 and height > padding * 2:
        left = min(point[1] for point in frame)
        top = min(point[2] for point in frame)
        right = max(point[1] for point in frame)
        bottom = max(point[2] for point in frame)
        # Slight pad so edge seats are not clipped.
        pad = max((right - left) * 0.04, (bottom - top) * 0.04, 2.0)
        left -= pad
        top -= pad
        right += pad
        bottom += pad
        scale, offset_x, offset_y = _fit(left, top, right, bottom, width, height, padding)
        # Draw only seats that land in the framed house (side islands stay out).
        drawn = [
            point
            for point in points
            if left <= point[1] <= right and top <= point[2] <= bottom
        ]
        placed_seats = tuple(
            PlacedSeat(
                key=key,
                x=offset_x + (x - left) * scale,
                y=offset_y + (y - top) * scale,
                venue_x=x,
                venue_y=y,
                sellable=sellable,
            )
            for key, x, y, sellable in drawn
        )
        names = {
            str(block.get("key") or block.get("block_key") or ""): str(
                block.get("name") or block.get("label") or block.get("block_name") or ""
            )
            for block in blocks or []
        }
        hulls: dict[str, list[float]] = {}
        for seat in placed_seats:
            box = hulls.setdefault(seat.key, [seat.x, seat.y, seat.x, seat.y])
            box[0] = min(box[0], seat.x)
            box[1] = min(box[1], seat.y)
            box[2] = max(box[2], seat.x)
            box[3] = max(box[3], seat.y)
        placed_blocks = tuple(
            PlacedBlock(key=key, name=names.get(key) or key, box=(vals[0], vals[1], vals[2], vals[3]))
            for key, vals in hulls.items()
        )
        # The seat feed carries no stage — seatMeta is blockKey plus seat
        # positions and nothing else — so this is inferred rather than measured.
        # It used to be a fixed box 35% of the venue width pinned to the middle
        # of the bounding box, which matched no real stage and drifted off to
        # one side of the seating whenever a venue was not symmetric.
        #
        # Seats face the stage and posTop grows away from it (the same
        # assumption 무대 가까운 순 already sorts on), so the rows nearest the
        # front say where the stage is and how wide it reads.
        front_cut = top + (bottom - top) * STAGE_FRONT_BAND
        front_xs = [point[1] for point in drawn if point[2] <= front_cut]
        if not front_xs:
            front_xs = [point[1] for point in drawn]
        stage_left = offset_x + (min(front_xs) - left) * scale
        stage_right = offset_x + (max(front_xs) - left) * scale
        if stage_right - stage_left < STAGE_MIN_WIDTH:
            # A single narrow block at the front should not shrink the stage to
            # a sliver; keep it readable and centred on what it does span.
            middle = (stage_left + stage_right) / 2
            stage_left = middle - STAGE_MIN_WIDTH / 2
            stage_right = middle + STAGE_MIN_WIDTH / 2
        stage = (stage_left, padding + 4, stage_right, padding + 22)
        return VenueView(
            seats=placed_seats,
            blocks=placed_blocks,
            stage=stage,
            scale=scale,
            origin_left=left,
            origin_top=top,
            offset_x=offset_x,
            offset_y=offset_y,
        )

    placed = project_blocks(blocks, width, height, padding=padding)
    return VenueView(
        seats=(),
        blocks=tuple(placed),
        stage=None,
        scale=1.0,
        origin_left=0.0,
        origin_top=0.0,
        offset_x=0.0,
        offset_y=0.0,
    )


def live_block_keys(seats: Sequence[dict[str, Any]]) -> set[str]:
    """Blocks with anything on sale this round.

    Block granularity, deliberately. A seat's own `isExposable` says whether it
    is on sale *now*, and a 취켓팅 range is a boundary, not a snapshot: the seats
    inside it that are currently taken are the entire point, because those are
    the ones a cancellation comes from. Filtering the range per seat meant a box
    drawn tightly around a sold-out section watched nothing at all.

    What is worth distinguishing is a block with *nothing* on sale — D and E on
    26012673 are not being sold this round at any price, so no cancellation can
    ever appear there.
    """
    live: set[str] = set()
    for seat in seats or []:
        if not is_sellable(seat):
            continue
        key = str(seat.get("k") or seat.get("key") or seat.get("block_key") or "").strip()
        if key:
            live.add(key)
    return live


def is_sellable(seat: dict[str, Any]) -> bool:
    """Whether this seat is part of the sellable map for this round.

    seatMeta's `isExposable` does not mean "free" — a sold-out show still
    reports it true for every seat. False means the seat is not being sold at
    all this round, which is what a partial house looks like: 26012673 sells
    1F/2F A-C and reports 712 real seats across D, E and one side block with no
    grade and isExposable false throughout.

    The sketch carries `s: 0` for those and omits the key otherwise, so absence
    means sellable — the flag is written for the minority and the sketch travels
    through a state file that is already 300KB.
    """
    return seat.get("s", seat.get("sellable", 1)) not in (0, False)


def sellable_seats(seats: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [seat for seat in seats if is_sellable(seat)]


def seats_in_watch_rect(
    seats: Sequence[dict[str, Any]],
    rect: tuple[float, float, float, float] | None,
) -> list[dict[str, Any]]:
    if rect is None:
        return list(seats)
    band = normalize_rect(rect)
    kept = []
    for seat in seats:
        x = _num(seat.get("x", seat.get("posLeft")))
        y = _num(seat.get("y", seat.get("posTop")))
        if x is None or y is None:
            continue
        if point_in_rect(x, y, band):
            kept.append(seat)
    return kept


def block_keys_in_watch_rect(
    seats: Sequence[dict[str, Any]],
    rect: tuple[float, float, float, float] | None,
) -> list[str]:
    if rect is None:
        return []
    found: list[str] = []
    seen: set[str] = set()
    # Whole blocks, not individual seats. A range is a boundary: the taken seats
    # inside it are what 취켓팅 exists to watch. Only a block with nothing on
    # sale at all is skipped — nothing can free up there, and polling it costs a
    # request every sweep.
    live = live_block_keys(seats)
    for seat in seats_in_watch_rect(seats, rect):
        key = str(seat.get("k") or seat.get("key") or seat.get("block_key") or "").strip()
        if key not in live:
            continue
        if key and key not in seen:
            seen.add(key)
            found.append(key)
    return found


def keys_in_lasso(view: VenueView, lasso: tuple[float, float, float, float]) -> list[str]:
    """Blocks touched by a canvas lasso — used only as a polling hint."""
    venue = view.canvas_rect_to_venue(lasso)
    if view.seats:
        found: list[str] = []
        seen: set[str] = set()
        dead = {seat.key for seat in view.seats} - {
            seat.key for seat in view.seats if seat.sellable
        }
        for seat in view.seats:
            if seat.key in dead:
                continue
            if point_in_rect(seat.venue_x, seat.venue_y, venue) and seat.key not in seen:
                seen.add(seat.key)
                found.append(seat.key)
        return found
    band = normalize_rect(lasso)
    return [item.key for item in view.blocks if boxes_intersect(item.box, band)]


def key_at_point(view: VenueView, x: float, y: float) -> str | None:
    if view.seats:
        best: PlacedSeat | None = None
        best_d = CLICK_RADIUS_PX * CLICK_RADIUS_PX
        dead = {seat.key for seat in view.seats} - {
            seat.key for seat in view.seats if seat.sellable
        }
        for seat in view.seats:
            # A block with nothing on sale is drawn but not selectable; a sold
            # seat in a live block is very much selectable.
            if seat.key in dead:
                continue
            dx = seat.x - x
            dy = seat.y - y
            dist = dx * dx + dy * dy
            if dist <= best_d:
                best_d = dist
                best = seat
        return best.key if best else None
    hits = [item for item in view.blocks if item.box[0] <= x <= item.box[2] and item.box[1] <= y <= item.box[3]]
    if not hits:
        return None
    hits.sort(key=lambda item: (item.area, item.key))
    return hits[0].key


def merge_keys(current: Sequence[str], incoming: Sequence[str], *, additive: bool) -> list[str]:
    if additive:
        seen = list(current)
        have = set(current)
        for key in incoming:
            if key not in have:
                seen.append(key)
                have.add(key)
        return seen
    return list(incoming)


def remove_keys(current: Sequence[str], outgoing: Sequence[str]) -> list[str]:
    remove = set(outgoing)
    return [key for key in current if key not in remove]


def is_click(x0: float, y0: float, x1: float, y1: float, threshold: float = DRAG_CLICK_PX) -> bool:
    return abs(x1 - x0) < threshold and abs(y1 - y0) < threshold
