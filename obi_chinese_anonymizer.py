"""
Chinese-aware name handling for FakeEngine (obi_anonymizer.py).

Covers the bilingual "Latin Name （中文名）" pattern seen in obip1 staff-name
columns (creator_name/updater_name in clm_purchase_orders and similar): a
pinyin/Latin name followed by the person's real Chinese name in parentheses,
e.g.:

    Xiaoting Li （李晓婷）

The stock token-splitting logic in _gen_person treats this as three
whitespace-separated tokens and fakes each independently -- the bracketed
Chinese segment (parens included) is replaced whole by an unrelated fake
surname, and the middle "Li" token is faked as a first name. The two halves
end up describing two different fake people, and the parens are lost.

This module generates the Latin and Chinese halves from ONE seed pair
(surname + given name), so both halves consistently describe the same fake
identity, and reassembles them in the original bracket style.

Deliberately holds no state of its own (no cache, no mapping_xref writes) --
it is called from FakeEngine._gen_person with the engine's own seed function,
so global injectivity (self._used) and reuse-first persistence (mapping_xref
via self._persist) keep working exactly as they do for every other type.
"""
import re

_HAN_LO, _HAN_HI = '一', '鿿'

# Chinese-language equivalent of HARVEST_STOP (obi_anonymizer.py): bare generic/placeholder
# words that occasionally sit in a person/org column instead of a real name (e.g. '管理员'/
# Administrator, found in create_by alongside real names like '吴佳慧') -- left unchanged
# rather than faked, same rationale as 'admin'/'test'/'string' in the Latin stopword set.
CN_HARVEST_STOP = {'管理员', '系统', '测试', '未知', '默认', '无', '暂无', '客人', '游客'}

# Chinese equivalent of GENERIC_ENTITY_STOP (constants.py): common business/document
# vocabulary that can sit as its own isolated, delimiter-bounded Han-script token inside a
# filename or freetext cell (e.g. '..._测试文件.docx', '..._报价单_...') and would otherwise be
# mistaken for a company/person name by the freetext Han-script backstop (see scrub_post() in
# obi_anonymizer.py -- GLiNER has near-zero recall on Han-script entities, confirmed 0 hits at
# threshold 0.1 on a real vendor name embedded in a filename, so a deterministic token-level
# backstop fakes any short isolated Han run; this list is what keeps that backstop from also
# faking ordinary Chinese business nouns). Not morphological -- a flat curated list of both
# single terms and the compounds they commonly form, same philosophy as GENERIC_ENTITY_STOP.
CN_GENERIC_STOP = {
    # document / file types
    '文件', '测试文件', '附件', '报告', '报表', '清单', '说明', '备注', '草稿', '副本', '版本',
    '表格', '记录', '简报', '发票', '收据', '凭证', '证明', '模板', '范本', '样本',
    # pricing / commercial documents
    '报价', '报价单', '询价', '询价单', '合同', '协议', '框架协议', '框架', '标书', '投标',
    '中标', '订单', '采购单', '结算', '账单', '预算', '成本', '费用', '金额', '价格', '折扣',
    '税费', '税率',
    # process / project vocabulary
    '需求', '服务', '项目', '计划', '方案', '提案', '建议', '审批', '审核', '确认', '签署',
    '签约', '核对', '反馈', '沟通', '会议', '讨论', '跟进', '更新', '修订', '变更', '调整',
    '标注', '标签', 'annotation', '数据', '流程', '进度', '交付', '验收', '任务', '团队',
    '项目组', '负责人', '联系人', '迭代', '阶段', '提交', '商务', '版本号', '商务报价单',
    '最终报价确认', '版',
    # status / qualifier words
    '最终', '最终版', '初步', '临时', '正式', '内部', '外部', '完成', '已完成', '待定', '取消', '有效',
    '无效', '已签', '未签', '已审批', '待审批', '通过', '拒绝', '暂停', '进行中', '已发送',
    '已收到',
    # generic role/entity nouns (mirrors HARVEST_STOP's 'account'/'customer'/'vendor' etc.)
    '客户', '供应商', '供应', '公司', '企业', '厂商', '合作方', '甲方', '乙方', '代理商',
    # time periods (already usually broken up by adjacent digits, kept as a backstop)
    '年度', '季度', '月度', '本月', '本年', '今年', '去年', '明年',
}


def is_harvest_stop(s):
    return (s or '').strip() in CN_HARVEST_STOP


def is_generic_stop(s):
    return (s or '').strip() in CN_GENERIC_STOP


def is_han(s):
    """True if s contains at least one CJK Unified Ideograph (Han script)."""
    return any(_HAN_LO <= c <= _HAN_HI for c in (s or ''))


# Matches "<latin><ws>(（|\()<chinese>(）|\))<ws><email>?" with either full-width or
# half-width parens, optional inner spaces, and an OPTIONAL trailing email with no separator
# required before it (real data glues it straight on: "...（郭超辉）chaohui.guo@centific.com").
# Chinese segment allows internal whitespace/middle-dot (e.g. compound given names) but must be
# pure Han script once that whitespace is stripped.
_BILINGUAL_RE = re.compile(
    r'^\s*(?P<latin>[A-Za-z][A-Za-z.\-\' ]*?)\s*'
    r'(?P<open>[（(])\s*(?P<chinese>[一-鿿·\s]+?)\s*(?P<close>[）)])'
    r'\s*(?P<email>[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})?\s*$'
)


def split_bilingual(s):
    """'Xiaoting Li （李晓婷）' -> ('Xiaoting Li', '李晓婷', '（', '）', None).
    'Chaohui Guo （郭超辉）chaohui.guo@centific.com'
        -> ('Chaohui Guo', '郭超辉', '（', '）', 'chaohui.guo@centific.com').
    Returns None if s doesn't match the "<latin> (<chinese>) [email]" shape (e.g. plain
    Latin name, plain Chinese name, or Chinese-with-Latin-in-parens reversed order --
    callers fall back to the existing per-token logic for those)."""
    m = _BILINGUAL_RE.match(s or '')
    if not m:
        return None
    latin = m.group('latin').strip()
    chinese = re.sub(r'\s+', '', m.group('chinese'))
    if not latin or not chinese or not is_han(chinese):
        return None
    return latin, chinese, m.group('open'), m.group('close'), m.group('email')


def _pick_unique_cn_name(seed_fn, key_prefix, fk_zh, used):
    """Retry surname+given draws from Faker's zh_CN pool (399 surnames x 131 given names =~
    52k combos) until landing on one not already in `used` (FakeEngine's global injectivity
    ledger, shared with every other type -- person/org/email all rely on it elsewhere).
    WITHOUT this, the small pool causes severe collisions across a few thousand distinct real
    names (confirmed empirically: 725 different real names in one 6,045-row table all collided
    onto shared fakes, e.g. 9 different people all faked to '张红霞') -- the same bug class
    already known and fixed for English names (see FakeEngine._ensure_bigpool's own "Faker's
    ~1-3k pool is far too small" comment), just never applied here when this was built.
    `used` is the CALLER's set, mutated in place -- this module still owns no state itself.
    Returns (surname_han, given_han)."""
    for i in range(300):
        fk_zh.seed_instance(seed_fn(f'{key_prefix}:sur#{i}'))
        surname_han = fk_zh.last_name()
        fk_zh.seed_instance(seed_fn(f'{key_prefix}:giv#{i}'))
        given_han = fk_zh.first_name()
        k = (surname_han + given_han).strip().casefold()
        if k and k not in used:
            used.add(k)
            return surname_han, given_han
    # whole ~52k-combo pool exhausted (a very large table) -- extend the given-name space by
    # concatenating a second draw, same "compound fallback" idea as FakeEngine._pick_unique.
    for i in range(300, 5300):
        fk_zh.seed_instance(seed_fn(f'{key_prefix}:sur#{i}'))
        surname_han = fk_zh.last_name()
        fk_zh.seed_instance(seed_fn(f'{key_prefix}:giv#{i}'))
        given_han = fk_zh.first_name()
        fk_zh.seed_instance(seed_fn(f'{key_prefix}:giv2#{i}'))
        given_han = given_han + fk_zh.first_name()
        k = (surname_han + given_han).strip().casefold()
        if k and k not in used:
            used.add(k)
            return surname_han, given_han
    return surname_han, given_han   # truly exhausted -- last resort, accept the collision


def gen_bilingual_name(original, seed_fn, fk_zh, pypinyin_mod, used):
    """Generate a matched (chinese, latin) name pair from ONE seed pair, unique against `used`.

    `seed_fn(key)` must be FakeEngine._seed (or equivalent: deterministic
    str -> int). Draws a surname and a given name from Faker's zh_CN locale,
    then transliterates each to pinyin via `pypinyin_mod.lazy_pinyin` -- so
    the Chinese and Latin outputs are two renderings of the SAME fake
    person, not independently generated. Assembles the Latin half in "Given
    Surname" order (matching the source convention: "Xiaoting Li" = given
    name first, surname last) and the Chinese half in "Surname Given" order
    with no space (Chinese convention: "李晓婷").

    Returns None if faker/pypinyin aren't available -- caller should fall
    back to whatever it does when those deps are missing.
    """
    if fk_zh is None or pypinyin_mod is None:
        return None
    surname_han, given_han = _pick_unique_cn_name(seed_fn, 'bilingual:' + original, fk_zh, used)
    chinese = surname_han + given_han

    def pinyin_cap(han):
        py = ''.join(pypinyin_mod.lazy_pinyin(han))
        return py[:1].upper() + py[1:] if py else py

    latin = f"{pinyin_cap(given_han)} {pinyin_cap(surname_han)}"
    return chinese, latin


def format_bilingual(latin, chinese, open_paren, close_paren, email=None):
    s = f"{latin} {open_paren}{chinese}{close_paren}"
    return f"{s}{email}" if email else s


def split_email_local(local):
    """'ling.chen14' -> ('ling', 'chen', '14'); 'chaohui.guo' -> ('chaohui', 'guo', '').
    Real addedBy emails here follow given.surname[digits] (the trailing digits are an
    existing disambiguator in the source system, e.g. a second 'chen' hire needing 'chen14')
    -- preserved as-is onto the fake surname rather than regenerated, since it's not itself
    PII, just a collision suffix. Returns (None, None, None) if the local part doesn't split
    into exactly two dot-separated pieces (caller falls back to a generic email fake)."""
    parts = local.split('.')
    if len(parts) != 2:
        return None, None, None
    m = re.match(r'^([A-Za-z]+)(\d*)$', parts[1])
    if not m:
        return None, None, None
    return parts[0], m.group(1), m.group(2)


def gen_bilingual_email_local(given_latin, surname_latin, digit_suffix):
    """Fake email local-part for the bilingual-name-plus-email pattern, built from the SAME
    given/surname pinyin already generated for the Latin name half -- so 'Chaohui Guo' faked
    to 'Yong Liang' produces 'yong.liang', never an independently-generated local-part that
    could disagree with the name sitting right next to it in the same string."""
    return f"{given_latin.lower()}.{surname_latin.lower()}{digit_suffix}"


# Real Chinese city names -- a DIFFERENT real city, not a transliteration/placeholder, matching
# the same "different real place" convention obi_anonymizer.py already uses for the Latin CITIES
# pool. Deliberately a large pool (80 cities, not just the ~10 major ones) so FakeEngine._pick_
# unique's quadratic probe has enough room that two distinct real cities essentially never
# collide onto the same fake for any realistic table -- collision likelihood matters here in a
# way it doesn't for names/orgs, since this pool is tiny compared to the name corpus.
CN_CITIES = [
    '广州', '杭州', '南京', '武汉', '西安', '青岛', '大连', '厦门', '长沙', '郑州',
    '济南', '合肥', '昆明', '南昌', '福州', '哈尔滨', '沈阳', '石家庄', '太原', '兰州',
    '贵阳', '南宁', '海口', '银川', '西宁', '乌鲁木齐', '拉萨', '呼和浩特',
    '无锡', '苏州', '天津', '北京', '上海', '重庆', '成都', '深圳', '盐城', '常州',
    '徐州', '宁波', '温州', '嘉兴', '绍兴', '金华', '台州', '泉州', '漳州', '莆田',
    '潍坊', '烟台', '临沂', '淄博', '济宁', '洛阳', '开封', '新乡', '安阳', '襄阳',
    '宜昌', '株洲', '湘潭', '柳州', '桂林', '珠海', '佛山', '东莞', '惠州', '中山',
    '汕头', '湛江', '江门', '韶关', '芜湖', '蚌埠', '扬州', '泰州', '唐山', '保定',
    '长春', '吉林', '绵阳', '南通',
]


# Work-arrangement / delivery-mode descriptors seen sitting in otherwise-genuine city/location
# columns (e.g. sift_bus_city.name had '在线'/Online and '坐班兼职'/On-site-part-time mixed in
# with real cities like '北京'). These aren't place names at all -- faking them into a city
# would be actively wrong, not just imprecise, so they're left completely unchanged rather than
# routed through gen_chinese_location. Deliberately a specific denylist (not a broad heuristic)
# so a genuine place name is never accidentally skipped.
CN_LOCATION_SKIP = {
    '在线', '离线', '线上', '线下', '远程', '远程办公', '居家办公', '现场',
    '全职', '兼职', '坐班', '坐班兼职', '实习', '待定', '无', '不限',
}


def is_location_skip(s):
    """True if s is a known non-place work-mode/status descriptor, not a genuine city/location."""
    return (s or '').strip() in CN_LOCATION_SKIP


# Country/region names -- a country reference in freetext (e.g. '...中国大陆...', a filename
# noting which market a document covers) is not itself PII and should stay verbatim, matching
# the same policy Latin-script COUNTRY_HINT columns already get (a country is faked to a
# *different real* country only for a column explicitly typed 'country', never scrubbed out of
# running freetext). Used by the Han-script freetext backstop (scrub_post) so a bare country
# mention isn't mistaken for a company/person token.
CN_COUNTRY_STOP = {
    '中国', '中国大陆', '大陆', '内地', '香港', '澳门', '台湾', '日本', '韩国', '朝鲜',
    '美国', '英国', '法国', '德国', '意大利', '西班牙', '俄罗斯', '加拿大', '澳大利亚',
    '新加坡', '马来西亚', '印度', '印尼', '泰国', '越南', '菲律宾', '巴西', '墨西哥',
}


def is_country_stop(s):
    return (s or '').strip() in CN_COUNTRY_STOP


# Chinese org-name fake material -- combined head+tail (no space, matching real Chinese
# company-name shape, e.g. "云舟科技") so a Han-script org value doesn't leak into an English
# descriptor phrase like "Vertex Systems". FICTIONAL, deliberately -- mirrors ORG_HEAD's own
# approach (Vertex/Nimbus/Aperture: abstract, evocative, invented-sounding words, NOT drawn from
# real corporate naming vocabulary). An earlier version of this list used common Chinese
# auspicious-business words (华鑫/尚德/恒泰/正泰/联创/嘉和/通达...) -- exactly the vocabulary
# real companies draw from, and two were outright name-roots of actual well-known companies
# (正泰 = Chint Group, a major listed electrical-equipment maker; 尚德 = Suntech Power, a
# formerly major solar company). Replaced with poetic/nature imagery characters that are NOT
# standard Chinese corporate-naming vocabulary, checked against major Chinese company names.
# Second pass (user-verified, with English glosses + web-search spot checks): three more words
# were flagged and swapped -- 岚曜 (too close to an existing company name) -> 澄岳, 熠辰 (a common
# real business-name element) -> 清穹, 云岭 (a well-known place/cultural name for Yunnan, also
# used by real orgs) -> 霁川. No exact-name hits found for the replacements or for a spot-check
# of the remaining list (only unrelated small/local companies or phonetic near-misses, same
# tolerance level already accepted for the Latin ORG_HEAD pool's own overlaps).
CN_ORG_HEAD = ['云舟', '墨轩', '星澜', '川岭', '曜川', '岚汀', '潋川', '清穹', '珞川', '汀岚',
               '墨澈', '星岑', '云璃', '澹岚', '曦岭', '澄岳', '潋汀', '墨熠', '星潋', '霁川']
CN_ORG_TAIL = ['科技', '集团', '网络', '数据', '创新', '控股', '信息', '实业', '智能', '发展']


def gen_chinese_org(original, seed_fn, used, head=CN_ORG_HEAD, tail=CN_ORG_TAIL):
    """Fake for a Chinese org/company name -> a different Han-script brandable name
    (head+tail, no space), deterministic & consistent, unique against `used` (FakeEngine's
    global injectivity ledger). Mirrors FakeEngine._gen_org's single_run+descriptor behavior
    for Latin names, just kept in Han script. Primary pool is only 20 head x 10 tail = 200 raw
    combinations -- same collision risk class as the name pools (see _pick_unique_cn_name), and
    confirmed by measurement to already be 64% consumed (128/200) across tables anonymized so
    far, so a compound fallback (same vetted words, head+tail+tail = 2,000 combos) is needed for
    real headroom rather than waiting for it to actually collide. The OLD fallback
    (`head[base]+tail[0]+head[base+1]`) depended only on `base`, not on a retry counter or
    uniqueness check -- once the pool was full it would have silently handed out the SAME
    fallback value to every org sharing that base, i.e. real collisions with no signal."""
    L = len(head); T = len(tail)
    norm = original.strip().casefold()
    # Linear (not quadratic) offset over the FULL L*T space -- guarantees every one of the 200
    # raw combos is actually tried once before giving up (quadratic probing on a pool this small
    # skips many slots by construction, which was silently causing premature "exhaustion" into
    # the old non-unique fallback after only a handful of tries -- confirmed by stress-testing
    # 250 distinct orgs and finding severe repeat collisions well before the pool was full).
    base = seed_fn('org:' + original) % (L * T)
    for s in range(0, L * T):
        combo = (base + s) % (L * T)
        out = head[combo // T] + tail[combo % T]
        k = out.strip().casefold()
        if k and k != norm and k not in used:
            used.add(k)
            return out
    base2 = seed_fn('org2:' + original) % (L * T * T)
    for s in range(0, L * T * T):
        combo = (base2 + s) % (L * T * T)
        out = head[combo // (T * T)] + tail[(combo // T) % T] + tail[combo % T]
        k = out.strip().casefold()
        if k and k != norm and k not in used:
            used.add(k)
            return out
    return head[base % L] + tail[0] + head[(base + 1) % L] + tail[1]   # truly exhausted (~2,200
    # combos gone) -- last resort, accept the risk rather than erroring


def gen_chinese_fullname(original, seed_fn, fk_zh, used):
    """Fake for a Chinese full name with NO Latin annotation and no space (e.g. '李晓婷' on
    its own) -- Chinese convention is surname+given run together as one token, which the
    generic whitespace tokenizer in _gen_person sees as a single "first name" token and would
    fake via fk_zh.first_name() alone, silently dropping the surname (e.g. '李晓婷' -> '英').
    Splits the same way (surname + given, rejoined with no separator), but now via
    _pick_unique_cn_name so it's unique against `used` -- see that function's docstring for why
    a naive per-original hash pick collides badly with only ~52k surname x given combinations.
    Returns None if Faker's zh_CN locale isn't available -- caller falls back to the old
    (lossy) behaviour rather than erroring."""
    if fk_zh is None:
        return None
    surname_han, given_han = _pick_unique_cn_name(seed_fn, 'cnfull:' + original, fk_zh, used)
    return surname_han + given_han
