"""截图确认投影至原需求与 TravelBooking；不写正式行程，不把截图变成官方事实。"""

from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from app.common.envelope import ApiError
from app.schemas.business.preparation import TravelBooking
from app.schemas.screenshot import ScreenshotConfirmBody, ScreenshotConfirmVO, ScreenshotDocumentVO, ScreenshotEntity
from app.schemas.screenshot_input import ConfirmedPlaceInput, ScreenshotEvidence
from app.schemas.trip_requirements import FixedArrangement, RequiredPlace, requirements_issues


def _local_instant(day, time, zone):
    if day is None or time is None:
        raise ApiError(422, "请确认完整当地日期、开始和结束时间；截图缺失项不能自动补齐")
    naive = datetime.fromisoformat(f"{day.isoformat()}T{time}:00")
    tz = ZoneInfo(zone)
    # 拒绝夏令时跳跃中的不存在时刻与回拨歧义；不能静默选择 fold。
    valid = [
        candidate
        for fold in (0, 1)
        if (candidate := naive.replace(tzinfo=tz, fold=fold))
        .astimezone(ZoneInfo("UTC"))
        .astimezone(tz)
        .replace(tzinfo=None)
        == naive
    ]
    if not valid or len({v.utcoffset() for v in valid}) != 1:
        raise ApiError(422, "当地时间处于夏令时缺口或重复时段，请手工确认具体时刻")
    return valid[0]


def confirm_input(body: ScreenshotConfirmBody, document: ScreenshotDocumentVO, resolution: dict) -> ScreenshotConfirmVO:
    entry = next((e for e in document.entries if e.record_id == body.record_id), None)
    entity_raw = next((e for e in resolution["choices"] if e["entityId"] == body.entity_id), None)
    if entry is None or entity_raw is None:
        raise ApiError(422, "请从本次查询的地点列表中明确选择实体")
    entity = ScreenshotEntity.model_validate(entity_raw)
    req = body.requirements.model_copy(deep=True)
    options = req.constraint_planning
    if not options or options.country_code != entity.country_code:
        raise ApiError(422, "请先确认与地点一致的目的地国家、时区和计价币种")
    if (entry.polarity == "excluded" and body.disposition != "excluded") and not body.override_polarity:
        raise ApiError(422, "原图明确否定此地点；改为安排前请单独确认覆盖否定语义")
    if entry.polarity == "unclear" and not body.override_polarity:
        raise ApiError(422, "原图意图不明确，请单独确认这次采用的语义")
    if body.amount is not None and not body.currency:
        raise ApiError(422, "金额缺少币种，不能从符号或行程币种猜测")
    input_id = f"shot:{document.document_id[:24]}:{entry.record_id}"
    confirmed = {
        "name": resolution["name"],
        "entityId": entity.entity_id,
        "city": resolution["city"],
        "countryCode": entity.country_code,
        "disposition": body.disposition,
        "identitySource": entity.identity_source,
        "address": entity.address[:255],
    }
    for key, value in (
        ("date", body.local_date),
        ("endDate", body.end_date),
        ("startTime", body.start_time),
        ("endTime", body.end_time),
        ("timeZone", body.time_zone),
        ("amount", body.amount),
        ("currency", body.currency),
    ):
        if value is not None:
            confirmed[key] = str(value)
    evidence = ScreenshotEvidence(
        image_id=document.image_id,
        record_id=entry.record_id,
        width=document.width,
        height=document.height,
        extracted_at=document.extracted_at,
        extractor=document.extractor,
        fields=entry.fields,
        confirmed_fields=confirmed,
    )
    place = ConfirmedPlaceInput(
        input_id=input_id,
        name=resolution["name"],
        local_name=entity.name,
        item_type="transport" if entry.kind == "transport" else "attraction",
        aliases=list(dict.fromkeys([entity.name, *entity.aliases]))[:40],
        city=resolution["city"],
        address=entity.address[:255],
        country_code=entity.country_code,
        entity_id=entity.entity_id,
        latitude=entity.latitude,
        longitude=entity.longitude,
        source_url=entity.source_url,
        identity_source=entity.identity_source,
        retrieved_at=entity.retrieved_at,
        original_amount=body.amount,
        original_currency=body.currency,
        evidence=evidence,
    )
    booking = None
    if body.disposition in {"fixed", "booking"}:
        if entry.kind == "transport" and entity.entity_type not in {
            "station",
            "halt",
            "stop_position",
            "bus_station",
            "ferry_terminal",
            "aerodrome",
            "airport",
        }:
            raise ApiError(422, "交通资料需选择对应车站、港口或机场实体，不能使用景点或市中心坐标")
        if not body.time_zone or body.time_zone != options.time_zone:
            raise ApiError(422, "请确认与行程一致的 IANA 当地时区；跨时区交通需分别录入")
        start = _local_instant(body.local_date, body.start_time, body.time_zone)
        end = _local_instant(body.end_date, body.end_time, body.time_zone)
        if end <= start:
            raise ApiError(422, "结束应晚于开始；跨日车票请明确确认到达日期")
        booking = TravelBooking(
            id=input_id,
            kind="transport" if entry.kind == "transport" else "ticket",
            name=place.name,
            startAt=start,
            endAt=end,
            address=entity.address[:255],
            inputEvidence=evidence,
            timeZone=body.time_zone,
            originalAmount=str(body.amount) if body.amount is not None else None,
            originalCurrency=body.currency,
            note="截图经用户确认录入；不是平台出票或官方核验。地点与时段需出发前核实。",
        )
        if body.disposition == "fixed":
            assert body.start_time and body.end_time  # _local_instant 已核对并构造了两端时刻。
            if not body.start_date or not body.local_date:
                raise ApiError(422, "请先确认行程出发日期")
            day_no = (body.local_date - body.start_date).days + 1
            if not 1 <= day_no <= body.days:
                raise ApiError(422, "预约日期超出本次行程")
            if body.local_date != body.end_date:
                raise ApiError(422, "跨日车票可保存到已有行程的已订资料；单城求解暂不支持跨午夜固定段")
            if any(f.source_ref == input_id for f in req.fixed_arrangements):
                raise ApiError(409, "这条截图已录入固定安排，请在原需求表单修改，避免覆盖旧硬约束")
            req.fixed_arrangements.append(
                FixedArrangement(
                    day_no=day_no,
                    name=place.name,
                    item_type="transport" if entry.kind == "transport" else "attraction",
                    start_time=body.start_time,
                    end_time=body.end_time,
                    cost=body.amount if body.currency == options.currency else None,
                    address=entity.address[:255],
                    remark="用户确认的截图安排，事实待核实",
                    source_ref=input_id,
                )
            )
    if body.disposition != "booking":
        if any(p.name == place.name and p.entity_id != place.entity_id for p in req.place_inputs):
            raise ApiError(422, "不同馆区使用了同一名称，请在查询名称中补充馆区再确认")
        if any(p.input_id == input_id for p in req.place_inputs):
            raise ApiError(409, "这条截图已加入需求，请在原需求表单修改")
        req.place_inputs.append(place)
        if body.disposition == "required":
            req.required_places.append(RequiredPlace(constraint_id=input_id, name=place.name, source_ref=input_id))
        elif body.disposition == "excluded" and place.name not in req.excluded_places:
            req.excluded_places.append(place.name)
    issues = requirements_issues(req, days=body.days)
    if issues:
        raise ApiError(422, "；".join(issues))
    # 追加后的长度也经原 Schema 校验，不能绕过各列表上限。
    try:
        req = type(req).model_validate(req.model_dump())
    except ValidationError as exc:
        raise ApiError(422, "截图输入超过需求容量或字段范围，请精简后重试") from exc
    return ScreenshotConfirmVO(
        requirements=req,
        booking=booking,
        notice="已生成待采用输入；需求仍需重新确认，正式行程未改写。截图与地点查询不证明营业或票价。",
    )
