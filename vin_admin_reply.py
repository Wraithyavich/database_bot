from __future__ import annotations

import re
import urllib.parse
from dataclasses import replace

from vin_search import (
    VinFitment,
    VinRecord,
    VinSource,
    extract_vin,
    utc_now,
)


class VinAdminReplyError(ValueError):
    pass


FIELD_PATTERN = re.compile(r"^\s*([^:：]+?)\s*[:：]\s*(.*?)\s*$")
VALUE_SPLIT_PATTERN = re.compile(r"\s*[,;]\s*")
POSITION_ALIASES = {
    "левая": "Левая",
    "левый": "Левая",
    "правая": "Правая",
    "правый": "Правая",
    "верхняя": "Верхняя",
    "верхний": "Верхняя",
    "нижняя": "Нижняя",
    "нижний": "Нижняя",
    "передняя": "Передняя",
    "передний": "Передняя",
    "задняя": "Задняя",
    "задний": "Задняя",
    "турбина": "Турбина",
    "турбо": "Турбина",
    "turbo": "Турбина",
    "картридж": "Турбина",
}
VEHICLE_FIELDS = {
    "марка": "make",
    "make": "make",
    "модель": "model",
    "model": "model",
    "год": "model_year",
    "model year": "model_year",
    "двигатель": "engine",
    "engine": "engine",
    "мощность": "power_kw",
    "мощность квт": "power_kw",
    "power": "power_kw",
}
ADMIN_REPLY_MARKERS = frozenset(
    set(POSITION_ALIASES)
    | set(VEHICLE_FIELDS)
    | {
        "oem",
        "оем",
        "oem левая",
        "oem правая",
        "oem верхняя",
        "oem нижняя",
        "oem передняя",
        "oem задняя",
        "источник",
        "source",
        "комментарий",
        "примечание",
    }
)
ADMIN_CONFIRMATION_PATTERN = re.compile(
    r"^\s*(?:подтверждаю|подтвердить|верно|сохранить)\s*[.!]?\s*$",
    re.IGNORECASE,
)


def is_admin_reply_candidate(text: str) -> bool:
    for raw_line in text.splitlines():
        match = FIELD_PATTERN.match(raw_line)
        if match and _normalize_label(match.group(1)) in ADMIN_REPLY_MARKERS:
            return True
    return False


def is_admin_confirmation(text: str) -> bool:
    return ADMIN_CONFIRMATION_PATTERN.fullmatch(text) is not None


def confirm_admin_vin_record(record: VinRecord) -> VinRecord:
    if not record.fitments or not any(
        fitment.oem_numbers or fitment.turbo_numbers
        for fitment in record.fitments
    ):
        raise VinAdminReplyError(
            "Для этого VIN пока нет найденных номеров, которые можно подтвердить."
        )
    note = "Результат подтверждён администратором."
    notes = record.notes
    if note not in notes:
        notes = " ".join(part for part in (notes, note) if part)
    return replace(
        record,
        status="verified",
        notes=notes,
        verified_at=utc_now(),
    )


def parse_admin_vin_reply(
    text: str,
    *,
    vin: str,
    base_record: VinRecord | None = None,
) -> VinRecord:
    normalized_vin = extract_vin(vin)
    if normalized_vin is None:
        raise VinAdminReplyError("Некорректный VIN.")
    if base_record is not None and base_record.vin != normalized_vin:
        raise VinAdminReplyError("VIN в ответе не совпадает с заявкой.")

    vehicle_updates: dict[str, str] = {}
    positions: dict[str, dict[str, list[str]]] = {}
    source_url = ""
    notes = ""

    for raw_line in text.splitlines():
        match = FIELD_PATTERN.match(raw_line)
        if not match:
            continue
        label = _normalize_label(match.group(1))
        value = " ".join(match.group(2).split())
        if not value or label == "vin":
            continue

        vehicle_field = VEHICLE_FIELDS.get(label)
        if vehicle_field is not None:
            vehicle_updates[vehicle_field] = value[:200]
            continue

        position = POSITION_ALIASES.get(label)
        if position is not None:
            positions.setdefault(
                position,
                {"oem_numbers": [], "turbo_numbers": []},
            )["turbo_numbers"].extend(_parse_numbers(value))
            continue

        if label == "oem" or label == "оем":
            position = "Турбина"
        elif label.startswith("oem ") or label.startswith("оем "):
            raw_position = label.split(" ", 1)[1]
            position = POSITION_ALIASES.get(raw_position)
            if position is None:
                continue
        else:
            position = None

        if position is not None:
            positions.setdefault(
                position,
                {"oem_numbers": [], "turbo_numbers": []},
            )["oem_numbers"].extend(_parse_numbers(value))
            continue

        if label in {"источник", "source"}:
            source_url = _validate_source_url(value)
        elif label in {"комментарий", "примечание"}:
            notes = value[:1000]

    fitments = tuple(
        VinFitment(
            position=position,
            oem_numbers=_unique(values["oem_numbers"]),
            turbo_numbers=_unique(values["turbo_numbers"]),
            articles=(),
            evidence="Ручная проверка администратора.",
        )
        for position, values in positions.items()
        if values["oem_numbers"] or values["turbo_numbers"]
    )
    if not fitments:
        raise VinAdminReplyError(
            "Не найдено ни одного номера. Добавьте строку вида "
            "«Левая: KP39-015» или «OEM: A6560900380»."
        )

    base = base_record or VinRecord(vin=normalized_vin, status="pending")
    sources = base.sources
    if source_url:
        manual_source = VinSource(
            label="Источник ручной проверки",
            url=source_url,
        )
        if manual_source not in sources:
            sources += (manual_source,)

    manual_note = "Результат подтверждён администратором."
    combined_notes = " ".join(
        part for part in (notes, manual_note) if part
    )
    return replace(
        base,
        status="verified",
        make=vehicle_updates.get("make", base.make),
        model=vehicle_updates.get("model", base.model),
        model_year=vehicle_updates.get("model_year", base.model_year),
        engine=vehicle_updates.get("engine", base.engine),
        power_kw=vehicle_updates.get("power_kw", base.power_kw),
        fitments=fitments,
        sources=sources,
        notes=combined_notes,
        verified_at=utc_now(),
    )


def format_admin_notification(
    record: VinRecord,
    *,
    failure_detail: str = "",
) -> str:
    vehicle = " ".join(part for part in (record.make, record.model) if part)
    details = " / ".join(
        part
        for part in (record.model_year, record.engine, record.power_kw)
        if part
    )
    lines = [
        "🛠 VIN требует ручной проверки",
        f"VIN: {record.vin}",
    ]
    if vehicle:
        lines.append(f"Автомобиль: {vehicle}")
    if details:
        lines.append(f"Год / двигатель / мощность: {details}")
    if failure_detail:
        lines.append(f"Причина: {failure_detail}")
    lines.extend(
        [
            "",
            "Ответьте именно на это сообщение, например:",
            "Левая: KP39-015",
            "Правая: KP39-020",
            "OEM левая: A6560900380",
            "Источник: https://...",
            "Комментарий: при необходимости",
            "",
            "Достаточно указать хотя бы одну турбину или OEM.",
        ]
    )
    return "\n".join(lines)


def _normalize_label(value: str) -> str:
    return " ".join(value.strip().lower().replace("ё", "е").split())


def _parse_numbers(value: str) -> list[str]:
    result = []
    for item in VALUE_SPLIT_PATTERN.split(value):
        cleaned = " ".join(item.split()).strip(" .")
        if cleaned and len(cleaned) <= 100:
            result.append(cleaned)
    return result


def _unique(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _validate_source_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise VinAdminReplyError(
            "Источник должен быть полной ссылкой http:// или https://."
        )
    return value
