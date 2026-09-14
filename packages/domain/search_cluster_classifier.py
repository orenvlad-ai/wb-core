"""Offline pure classifier. No I/O, network, labels, statistics or campaign status."""
import re,unicodedata
VERSION='1.0.0'
from packages.contracts.search_cluster_cleaner import Profile, CleanerError, query_hash

def norm(q):
 q=unicodedata.normalize('NFKC',q).lower().replace('ё','е')
 q=re.sub(r'(?<!\w)(?:стикл|cтекл)', 'стекл', q)
 for p,v in [(r'айфон[а-я]*|аифон|iphonе|iphone|iphon|aiphone|айфоне',' iphone '),(r'промакс|про\s*макс|pro\s*max|promax',' promax '),(r'\bпро\b',' pro '),(r'\bмакс\b',' max '),(r'\bмини\b',' mini '),(r'\bплюс\b',' plus '),(r'\bэйр\b|\bаир\b',' air '),(r'анти\s+шпион','антишпион')]:q=re.sub(p,v,q)
 q=re.sub(r'(?<=\d)(?=pro|max|mini|plus|[eе]\b)',' ',q)
 q=re.sub(r'\b([0-9]+)\s*iphone\s*(pro|max|mini|plus)',r'\1 \2',q)
 q=re.sub(r'(\d)\s+е\b',r'\1 e',q)
 return re.sub(r'\s+',' ',q).strip()

def models(q):
 found=[];spans=[]
 q=re.sub(r'(?<!\w)\d+\s*[xх×]\s*\d+(?!\w)|(?<!\w)\d+[dh](?!\w)',lambda m:' '*len(m[0]),q)
 for m in re.finditer(r'(?<![\w])([1-9]|1[0-9])(?:\s*(promax|pro\s+max|pro|plus|mini|e|max))?(?![\w])',q):
  val=m[1]+(' '+m[2].replace('pro max','promax') if m[2] else '')
  found.append(val);spans.append(m.span())
 for m in re.finditer(r'\bair\b',q):found.append('air');spans.append(m.span())
 for m in re.finditer(r'(?<![\w-])(?:x(?:\s+max)?|se)(?![\w-])',q):found.append(m[0]);spans.append(m.span())
 return set(found),sorted(spans)

BRANDS=r'\b(?:uniq|nillkin|remax|глазурь|glazur|magic\s+glass\s+store)\b'
PRODUCT=r'ст[её]кл|стекол|брон|защит|антишпион|\bglass\b'
# Closed vocabulary for positive admission, independent of campaign/query labels.
VOCAB=r'x|se|iphone|pro|promax|max|mini|plus|air|e|на|для|с|со|без|и|в|во|от|по|к|все|из|телефон[а-я]*|смартфон[а-я]*|экран[а-я]*|стекл[а-я]*|стекол|брон[а-я]*|защит[а-я]*|антишпион[а-я]*|матов[а-я]*|прозрачн[а-я]*|глянцев[а-я]*|антиблик[а-я]*|авто[а-я]*|установ[а-я]*|накле[а-я]*|покле[а-я]*|рам[а-я]*|бокс[а-я]*|аксессуар[а-я]*|оригинал[а-я]*|премиум|горилла|gorilla|glass|hd|d|h|черн[а-я]*|бел[а-я]*|полноэкранн[а-я]*|противоударн[а-я]*|защитка|защитки|magic|protection|комплект[а-я]*|набор[а-я]*|легк[а-я]*|шт|штук[а-я]*|упаковк[а-я]*|салфетк[а-я]*|окантовк[а-я]*|безрамочн[а-я]*'

# Known non-model words only unblock BROAD exclusion; they never grant admission.
BROAD_WORDS=r'app|one|анти|пыл[а-я]*|apple|система|размер[а-я]*|чтобы|не|видно|потел[а-я]*|грамм|электроника|мобильн[а-я]*|поверхност[а-я]*|покрыти[а-я]*|скорость|сама|товар[а-я]*|кита[а-я]*|премиальн[а-я]*|мальчик[а-я]*|подрост[а-я]*|матированн[а-я]*|под'

def classify(query,profile):
 try:
  query_hash(query)
  profile = profile if isinstance(profile, Profile) else Profile.parse(profile)
 except CleanerError as exc:
  return dict(verdict='review',rule=exc.code,reason=str(exc),normalized='',models=[],compatible=[])
 profile=profile.as_dict()
 q=norm(query)
 # Quantities/measurements are properties, not phone generations. Mask them before models.
 q=re.sub(r'(?<!\w)(?:\d+[.,]\d+|\d+)\s*(?:мм|mm|см|cm|шт(?:ук[аи]?)?|штук[а-я]*|d|h)(?!\w)', ' ', q)
 q=re.sub(r'\s+',' ',q).strip()
 ms,spans=models(q);compatible=set(profile['models'])
 facts={'normalized':q,'models':sorted(ms),'compatible':sorted(compatible)}
 def result(verdict,rule,reason):return dict(verdict=verdict,rule=rule,reason=reason,**facts)
 if not compatible or profile.get('category')!='phone_screen_glass':return result('review','PROFILE','Нет поддержанного профиля совместимости')
 product=bool(re.search(PRODUCT,q));accessories=bool(re.search(r'аксессуар',q))
 # A bare device or positively requested case remains a different object even
 # when its suffix/properties are unknown. Negated object-only phrases abstain.
 if not product and not accessories and not re.search(r'\b(?:только\s+)?не\s+(?:чехол|стекл)',q):
  return result('exclude','NO_PRODUCT','Нет обозначения защиты или аксессуара')
 # For a model-specific product, negating a property cannot supply a missing
 # model. Bare broad requests still fail the independently decisive model rule.
 if (ms or re.search(r'\d|'+BRANDS,q) or re.search(r'\b(?:только\s+)?не\s+(?:чехол|стекл)',q)) and re.search(r'\bне\b|\bбез\s+(?!черн[а-я]*\s+(?:рам|окантовк)|рам|окантовк)\S+',q):
  return result('review','NEGATION','Отрицание требует отдельного разбора')
 if re.search(r'\b(?:ultra|ультра|pr|prom|pm|pmax|mx|maxx|пм)\b',q):
  return result('review','MODEL_UNKNOWN','Неописанный суффикс модели')
 if re.search(BRANDS,q):return result('exclude','BRAND','Явно назван запрещённый чужой бренд')
 product=bool(re.search(PRODUCT,q));accessories=bool(re.search(r'аксессуар',q))
 if re.search(r'\bэкран[а-я]*\b',q) and not product:return result('exclude','SCREEN','Экран без указания на защиту')
 if re.search(r'\bрамк[а-я]*\b',q) and not product:return result('exclude','FRAME_ONLY','Рамка без указания на стекло')
 if re.search(r'бокс[а-я]*\s+с\s+iphone',q) and not product:return result('exclude','BOX_PHONE','Бокс с телефоном, не стекло')
 if re.search(r'упаковк|салфетк|шнур',q):
  if product and re.search(r'\b(?:в\s+упаковк|с\s+салфетк)',q):pass
  else:return result('exclude','SUPPLIES','Запрошены упаковка, салфетки или защита шнура')
 if re.search(r'штор|покрывал|глушител|ларгус|спортив|для картин|чайник|тумба|очки',q):return result('exclude','OTHER_CATEGORY','Явно другая категория товара')
 category_q=re.sub(r'\bпод\s+чехол\b|\bвырез\s+под\s+камер[а-я]*\b', '', q)
 if re.search(r'чехол|чехлы|кабел|заряд|дисплей|аккумулятор|пленк|плёнк|гидрогел|планшет|макбук|ipad|macbook|samsung|xiaomi|redmi|poco|tecno|infinix|honor|huawei|realme|oppo|самсунг|реалми|айпад|монитор',category_q):
  if re.search(r'чехол|чехлы',category_q):return result('exclude','CASE','Запрос на чехол, в том числе совместно со стеклом')
  return result('exclude','OTHER_PRODUCT','Явно другой товар или семейство устройств')
 if re.search(r'камер|линз|задн',category_q):return result('exclude','OTHER_SURFACE','Защита камеры или задней поверхности')
 if not product and not accessories:return result('exclude','NO_PRODUCT','Нет обозначения защиты или аксессуара; модель не додумывается')
 # Unknown model numbers/suffixes must not be truncated to a known base model.
 if re.search(r'(?<!\w)\d{2,}(?!\w)',q):
  if any(int(n)>19 for n in re.findall(r'(?<!\w)\d{2,}(?!\w)',q)):
   return result('review','MODEL_UNKNOWN','Неописанная модель или числовое свойство')
 # Unrecognised numeric/model suffix: do not silently downgrade to a base phone.
 residual=re.sub(r'(?<!\w)\d+\s*[xх×]\s*\d+(?!\w)|(?<!\w)\d+[dh](?!\w)',lambda m:' '*len(m[0]),q)
 for a,b in reversed(spans):residual=residual[:a]+' '+residual[b:]
 if re.search(r'(?<!\w)\d+(?:[a-zа-я]+|\*)|(?<!\w)(?:pr|prom|pm|pmax|mx|maxx|пм)(?!\w)',residual):
  if q=='18*':return result('exclude','BROAD','Нет полного обозначения устройства или товара')
  return result('review','MODEL_UNKNOWN','Нераспознанное обозначение модели или числовое свойство')
 if not product and not accessories:return result('exclude','NO_PRODUCT','Модель или иная фраза без обозначения защиты или аксессуара')
 if ms-compatible:
  if ms&compatible:return result('review','MODEL_MIX','Вместе указаны совместимые и другие модели')
  return result('exclude','MODEL_WRONG','Все явно распознанные модели несовместимы')
 if not ms:
  unknown=[w for w in re.findall(r'[a-zа-я]+',q) if not re.fullmatch(VOCAB+'|'+BROAD_WORDS,w)]
  if unknown:return result('review','MODEL_VOCAB_UNKNOWN','Не распознана модель; неизвестные слова: '+', '.join(sorted(set(unknown))))
  if re.search(r'\b(?:pro|promax|max|mini|plus|iphone)\b',q) and re.search(r'\d',q):return result('review','MODEL_UNKNOWN','Неполное обозначение модели')
  return result('exclude','BROAD','Не указана совместимая модель узкоспецифичного товара')
 if not product and not accessories:return result('exclude','NO_PRODUCT','Модель без обозначения защиты или аксессуара')
 if re.search(r'\bне\b|без\s+(?:антишпион|матов|прозрач)',q):return result('review','NEGATION','Отрицание свойства требует отдельного правила')
 unknown=[w for w in re.findall(r'[a-zа-я]+',category_q) if not re.fullmatch(VOCAB,w)]
 if unknown:return result('review','VOCAB_UNKNOWN','Неописанные слова: '+', '.join(sorted(set(unknown))))
 if re.search(r'без\s+(?:черн[а-я]*\s+)?(?:рам[а-я]*|окантовк[а-я]*)|безрамочн',q) and profile['frame']=='black':return result('exclude','NO_FRAME','Запрошено стекло без рамки, у товара чёрная рамка')
 if re.search(r'без\s+(?:антишпион|матов|прозрач)',q):return result('review','NEGATION','Отрицание свойства требует отдельного правила')
 anti=bool(re.search(r'антишпион',q));matte=bool(re.search(r'матов',q));clear=bool(re.search(r'прозрачн|глянцев',q))
 if sum([anti,matte,clear])>1:return result('review','PROPERTY_MIX','Несколько явно названных типов покрытия')
 if anti and profile['kind']!='anti':return result('exclude','COATING','Явно запрошен антишпион для другого покрытия')
 if matte and profile['kind']!='matte':return result('exclude','COATING','Явно запрошено матовое стекло для другого покрытия')
 if clear and profile['kind']!='clean':return result('exclude','COATING','Явно запрошено прозрачное/глянцевое стекло для другого покрытия')
 if accessories:return result('allow','ACCESSORIES','Аксессуары и совместимая модель разрешены интервью')
 if re.search(r'gorilla|горилла',q):return result('allow','GORILLA','Разрешённое исключение Gorilla при совместимости')
 if q.endswith(' без'):return result('allow','UNFINISHED_WITHOUT','Не додумываем свойство после оборванного без')
 return result('allow','COMPATIBLE','Совместимая модель, защита экрана, конфликта свойств нет')
