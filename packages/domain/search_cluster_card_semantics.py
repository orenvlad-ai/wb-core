"""Deterministic classifier-relevant projection of an official WB content card.

Only facts that can change the cleaner's product profile belong here. A new
unknown representation fails closed instead of silently inheriting the saved
profile. This version covers the reviewed phone-glass corpus.
"""
from __future__ import annotations

import re

from packages.contracts.search_cluster_cleaner import MODEL_CATALOG, CleanerError

PROJECTION_VERSION='phone_glass_v1'
_MODEL=re.compile(r'^(?:apple\s*)?iphone\s*(?:(1[3-8])\s*(pro\s*max|promax|pro|e)?|(?:(?:17\s*)?air))$',re.I)
_COMPETITOR=re.compile(r'\b(?:samsung|galaxy|xiaomi|redmi|huawei|honor|realme|pixel)\b',re.I)
_OTHER_PRODUCT=re.compile(r'^\s*(?:защитн\w*\s+)?(?:чехол|пл[её]нк\w*|case|film)\b',re.I)
_ANTI=re.compile(r'анти[\s-]*шпион|anti[\s-]*spy|\bprivacy\b',re.I)
_MATTE=re.compile(r'матов\w*|\bmatte\b',re.I)
_CLEAN=re.compile(r'(?:прозрачн\w*\s+(?:защитн\w*\s+)?стекл\w*|стекл\w*\s+прозрачн\w*|\bclear\s+glass\b)',re.I)
_STRUCTURED_CLEAR=re.compile(r'\bпрозрачн(?:ый|ое|ая|ые|ого|ому|ым|ом|ую|ых|ыми)\b',re.I)
_EXPLICIT_KIND_NAME=re.compile(r'^(?:(?:тип|вид)\s+(?:защитного\s+)?стекла|(?:тип|вид)\s+покрытия)$',re.I)
_GENERIC_KIND_NAME=re.compile(r'^(?:покрытие|эффект)$',re.I)
_NO_FRAME=re.compile(r'без\s+рамк\w*|\bno[\s-]*frame\b',re.I)
_BLACK_FRAME=re.compile(r'(?:черн\w*|чёрн\w*|black)\s+рамк\w*|рамк\w*\s+(?:черн\w*|чёрн\w*|black)',re.I)
_PHONE_TOKEN=r'(?:\d{1,2}\s*(?:pro\s*max|promax|pro|e|air)?|[A-Za-z][A-Za-z0-9]*)'
_MODEL_LIST=re.compile(r'iphone\s*'+_PHONE_TOKEN+r'(?:\s*[/,_]\s*(?:iphone\s*)?'+_PHONE_TOKEN+r')*',re.I)


def _unresolved(reason:str) -> None:
    raise CleanerError('current_card_semantics_unavailable',reason,409)


def _single_characteristic(card:dict, number:int, name:str):
    matches=[row for row in card['characteristics'] if row['id']==number or row['name'].strip().casefold()==name.casefold()]
    if len(matches)!=1 or matches[0]['id']!=number:
        _unresolved(f'Карточка WB: характеристика «{name}» отсутствует или неоднозначна')
    value=matches[0]['value']
    if not isinstance(value,list) or not value or any(not isinstance(item,str) for item in value):
        _unresolved(f'Карточка WB: характеристика «{name}» не подтверждена')
    return value


def _model(value:str) -> str:
    normalized=' '.join(value.split())
    match=_MODEL.fullmatch(normalized)
    if not match:_unresolved('Карточка WB: совместимость содержит неподдерживаемую модель')
    number,suffix=match.groups()
    if number:
        key=number+(' '+suffix.lower().replace(' ','') if suffix and 'max' in suffix.lower() else
                    ' '+suffix.lower() if suffix else '')
    else:key='air'
    if key not in MODEL_CATALOG:_unresolved('Карточка WB: совместимость содержит неподдерживаемую модель')
    return key


def _check_model_claims(text:str, models:list[str], label:str, *, required:bool=False) -> None:
    groups=list(_MODEL_LIST.finditer(text))
    if required and not groups:_unresolved(f'Карточка WB: {label} не подтверждает совместимость')
    for group in groups:
        for mention in re.split(r'\s*[/,_]\s*',group.group()):
            if _model(mention if mention.lower().startswith('iphone') else 'iPhone '+mention) not in models:
                _unresolved(f'Карточка WB: {label} противоречит совместимости')
        remainder=text[group.end():]
        if (re.match(r'[A-Za-z0-9]',remainder)
                or re.match(r'(?i)\s+(?:ultra|plus|mini|lite|se|fold|edge|nano|xs|xr)\b',remainder)):
            _unresolved(f'Карточка WB: {label} содержит неподдерживаемую модель')


def project_card(card:dict, *, require_subject:bool=False) -> dict:
    """Return the exact category/models/kind/frame used by cleaner rules."""
    if not isinstance(card,dict) or not isinstance(card.get('characteristics'),list):
        _unresolved('Карточка WB: характеристики недоступны')
    ids=set()
    for row in card['characteristics']:
        if (not isinstance(row,dict) or not {'id','name','value'}.issubset(row) or type(row['id']) is not int
                or not isinstance(row['name'],str) or row['id'] in ids):
            _unresolved('Карточка WB: характеристики неоднозначны')
        ids.add(row['id'])
    title=card.get('title') if card.get('title') is not None else ''
    description=card.get('description') if card.get('description') is not None else ''
    vendor=card.get('vendor_code') if card.get('vendor_code') is not None else ''
    if not all(isinstance(value,str) for value in (title,description,vendor)):
        _unresolved('Карточка WB: название, описание или артикул недоступны')
    subject_id=card.get('subject_id')
    if require_subject and subject_id is None:
        _unresolved('Карточка WB: категория товара не подтверждена официальным ответом')
    if subject_id is not None and subject_id!=1571:
        _unresolved('Карточка WB: категория товара изменилась')
    if subject_id is None and not re.search(r'\b(?:защитн\w*\s+)?стекл\w*\b|\b(?:screen\s+)?glass\b',title,re.I):
        _unresolved('Карточка WB: тип защитного стекла не подтверждён')
    if _OTHER_PRODUCT.search(title):
        _unresolved('Карточка WB: название указывает другой тип товара')
    if _COMPETITOR.search(title) or _COMPETITOR.search(vendor):
        _unresolved('Карточка WB: указана другая марка телефона')

    manufacturer=_single_characteristic(card,12223252,'Производитель телефона')
    if len(manufacturer)!=1 or manufacturer[0].strip().casefold()!='apple':
        _unresolved('Карточка WB: производитель телефона изменился')
    compatibility=_single_characteristic(card,746,'Совместимость')
    models=[]
    for value in compatibility:
        if value.strip().casefold()=='apple':continue
        models.append(_model(value))
    if not models or len(set(models))!=len(models):
        _unresolved('Карточка WB: совместимость неполная или неоднозначная')
    _check_model_claims(title,models,'название')
    _check_model_claims(vendor,models,'артикул')
    description_compatibility=re.search(r'(?i)(?:совместимость|модель\s+телефона)\s*[:—-]\s*([^.;\n]+)',description)
    if description_compatibility:
        declared=description_compatibility.group(1)
        _check_model_claims(declared,models,'описание',required=True)
        residue=_MODEL_LIST.sub(' ',declared)
        residue=re.sub(r'(?i)\b(?:apple|and)\b|\bи\b',' ',residue)
        if re.sub(r'[\s,;/_()+-]','',residue):
            _unresolved('Карточка WB: описание содержит неподтверждённую совместимость')

    frame_value=_single_characteristic(card,195594,'Цвет рамки')
    if len(frame_value)!=1:_unresolved('Карточка WB: цвет рамки неоднозначен')
    frame_text=frame_value[0].strip().casefold().replace('ё','е')
    if frame_text in {'черный','черная','черное','black'}:frame='black'
    elif frame_text in {'без рамки','нет','отсутствует','none'}:frame='none'
    else:_unresolved('Карточка WB: рамка не подтверждена')
    if (_NO_FRAME.search(title) or _NO_FRAME.search(vendor)) and frame!='none':
        _unresolved('Карточка WB: сведения о рамке противоречат друг другу')
    if (_BLACK_FRAME.search(title) or _BLACK_FRAME.search(vendor)) and frame!='black':
        _unresolved('Карточка WB: сведения о рамке противоречат друг другу')

    claims=set()
    if re.search(r'^\s*\(\s*anti[\s-]*spy\s*\)',vendor,re.I):claims.add('anti')
    elif re.search(r'^\s*\(\s*matte\s*\)',vendor,re.I):claims.add('matte')
    elif re.search(r'^\s*\(\s*clean\s*\)',vendor,re.I) or re.search(r'^smk_iphone',vendor,re.I):claims.add('clean')
    if _ANTI.search(title):claims.add('anti')
    if _MATTE.search(title):claims.add('matte')
    if _CLEAN.search(title):claims.add('clean')
    # Free-form description may compare coverings or discuss transparency as
    # an image quality. Read only an explicit labelled product declaration.
    labelled=re.search(r'(?i)(?:тип\s+стекла|тип\s+покрытия)\s*[:—-]\s*([^.;\n]+)',description)
    if labelled:
        value=labelled.group(1).strip()
        if not re.match(r'(?i)(?:не\b|сравн|в\s+отличие|отличается\s+от)',value):
            found=set()
            if _ANTI.search(value):found.add('anti')
            if _MATTE.search(value):found.add('matte')
            if _CLEAN.search(value):found.add('clean')
            if len(found)!=1:_unresolved('Карточка WB: тип стекла в описании неоднозначен')
            claims.update(found)
    for row in card['characteristics']:
        name=row['name'].strip().casefold()
        explicit=bool(_EXPLICIT_KIND_NAME.fullmatch(name))
        generic=bool(_GENERIC_KIND_NAME.fullmatch(name))
        if row['id'] in {746,195594,12223252} or not (explicit or generic):continue
        values=row['value']
        if not isinstance(values,list) or not values or any(not isinstance(value,str) for value in values):
            _unresolved('Карточка WB: тип покрытия не подтверждён')
        for value in values:
            found=set()
            if _ANTI.search(value):found.add('anti')
            if _MATTE.search(value):found.add('matte')
            if (_CLEAN.search(value) or _STRUCTURED_CLEAR.search(value)
                    or re.search(r'обычн\w*\s+стекл\w*',value,re.I)):found.add('clean')
            if len(found)>1 or explicit and len(found)!=1:
                _unresolved('Карточка WB: тип покрытия не подтверждён')
            claims.update(found)
    if len(claims)!=1:_unresolved('Карточка WB: тип стекла не подтверждён или противоречив')
    return dict(version=PROJECTION_VERSION,category='phone_screen_glass',
                models=sorted(models),kind=claims.pop(),frame=frame)
