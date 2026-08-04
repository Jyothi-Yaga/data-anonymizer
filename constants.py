"""Shared constants for obi_anonymizer.py's free-text scrubbing pipeline.

Centralizes stop-lists and table-domain classification so a policy decision scoped to one
domain (resume/candidate-evaluation text) can't silently leak into another (CRM/sales data)
via the single shared scrub_post()/scrub_text() functions -- see
FIX_GUIDE_freetext_domain_scope_and_forced_company_map.md for the incident this fixes
(dyncrm_activity/dyncrm_leads leaking real client names like "Google"/"Meta" because a
resume-specific brand-stop list was being applied table-agnostically).
"""

# ── table domain classification ──────────────────────────────────────────────────────────
# Tables whose free-text columns are resume/candidate/job-matching text, where a well-known
# company mentioned as a past employer or required skill (e.g. "AWS experience", "worked at
# NVIDIA") is NOT the data subject's own identity and should stay real -- masking it corrupts
# evaluative text for no privacy benefit (explicit prior decision, see KNOWN_BRAND_STOP below,
# originally scoped to ResumeRating_Job_Candidate only).
#
# Every other table defaults to the general/CRM domain, where a brand name IS a client/
# competitor identity being protected and must always be anonymized regardless of how famous
# the company is.
RESUME_DOMAIN_TABLES = {
    'ResumeRating_Job_Candidate',
    'ResumeRating_Job_Requisition',
    'ResumeRating_Job_Resume_ParsingLog',
    'ResumeMapping_Resume_Record',
    'ResumeMapping_Resume_ParsingLog',
    'sift_bus_resume',
    'sift_bus_candidate',
    'sift_bus_candidate_status',
    'sift_bus_delivery_manager',
    'sift_bus_city',
    'sift_bus_rating_criteria',
    'sift_bus_customer',
    'sift_bus_position',
    'sift_bus_project',
}


def table_domain(table):
    """'resume' for candidate/job-matching tables, 'general' (CRM/everything else) otherwise.

    Strips a trailing _anonymized/_src/_clean/_script suffix first so this still resolves
    correctly regardless of which stage of the run/rename-dance the table name reflects."""
    base = table
    for suf in ('_anonymized', '_src', '_clean', '_script'):
        if base.endswith(suf):
            base = base[: -len(suf)]
            break
    return 'resume' if base in RESUME_DOMAIN_TABLES else 'general'


# ── generic job-title / degree / department / tech-tool vocabulary ──────────────────────────
# Applies in EVERY domain (unlike KNOWN_BRAND_STOP below) -- a job title or generic tech term
# is never real PII regardless of which table it sits in. Moved verbatim from
# obi_anonymizer.py, content unchanged.
GENERIC_ENTITY_STOP = {
    # roles / titles / seniority / C-suite & function acronyms
    'engineer', 'engineers', 'engineering', 'manager', 'managers', 'management', 'specialist',
    'specialists', 'intern', 'interns', 'internship', 'internships', 'analyst', 'analysts',
    'director', 'directors', 'officer', 'officers', 'lead', 'leads', 'leadership', 'leader',
    'leaders', 'coordinator', 'coordinators', 'developer', 'developers', 'scientist',
    'scientists', 'consultant', 'consultants', 'architect', 'architects', 'administrator',
    'administrators', 'representative', 'representatives', 'executive', 'executives', 'partner',
    'partners', 'recruiter', 'recruiters', 'researcher', 'researchers', 'nurse', 'nurses',
    'radiologist', 'radiologists', 'teacher', 'teachers', 'student', 'students', 'candidate',
    'candidates', 'sourcer', 'sourcers', 'moderator', 'moderators', 'technician', 'technicians',
    'teammate', 'teammates', 'graduate', 'graduates', 'undergraduate', 'undergraduates',
    'applicant', 'applicants', 'participant', 'participants', 'individual', 'individuals',
    'volunteer', 'volunteers', 'fresher', 'freshers', 'talent', 'talents', 'professional',
    'professionals', 'personnel', 'employee', 'employees', 'staff', 'contributor',
    'contributors', 'member', 'members', 'team', 'teams', 'hire', 'hires', 'tester', 'testers',
    'driver', 'drivers', 'operator', 'operators', 'agent', 'agents', 'associate', 'associates',
    'assistant', 'assistants', 'senior', 'junior', 'principal', 'sme', 'smes', 'tpm', 'spm',
    'sdet', 'sdets', 'sre', 'sres', 'qa', 'devops', 'hr', 'chro', 'ceo', 'cto', 'cfo', 'coo',
    'cmo', 'cpo', 'cxo', 'vp', 'svp', 'evp', 'avp', 'president', 'chief', 'head', 'colleague',
    'colleagues', 'worker', 'workers', 'workforce', 'position', 'positions', 'role', 'roles',
    'vacancy', 'vacancies', 'opening', 'openings', 'headcount', 'fte', 'ftes', 'backfill',
    'replacement', 'hiring', 'degree', 'degrees', 'bachelor', 'bachelors', "bachelor's",
    'master', 'masters', "master's", 'phd', 'ph.d', 'mba', 'doctorate', 'doctorates',
    'self-learner', 'learner', 'learners',
    # departments / functions / generic org descriptors
    'department', 'departments', 'program', 'programs', 'project', 'projects', 'platform',
    'platforms', 'service', 'services', 'solution', 'solutions', 'operations', 'operation',
    'marketing', 'sales', 'recruiting', 'sourcing', 'delivery', 'support', 'infra',
    'infrastructure', 'cloud', 'analytics', 'community', 'communities', 'ecosystem', 'industry',
    'sector', 'organization', 'organizations', 'enterprise', 'enterprises', 'business',
    'businesses', 'company', 'companies', 'agency', 'agencies', 'client',
    'clients', 'customer', 'customers', 'vendor', 'vendors', 'group', 'groups', 'unit', 'units',
    'division', 'divisions', 'function', 'functions', 'initiative', 'initiatives', 'committee',
    'committees', 'board', 'boards', 'office', 'offices', 'center', 'centers', 'centre',
    'centres', 'legal', 'finance', 'procurement', 'compliance', 'audit', 'security', 'logistics',
    'immigration', 'payroll', 'benefits', 'recruitment', 'channel', 'channels', 'user', 'users',
    'research', 'product', 'products', 'account', 'accounts', 'evaluation', 'evaluations',
    'pm', 'pms', 'vps', 'spms', 'tpms', 'coordination', 'analysis', 'annotation', 'annotator',
    'annotators', 'practice', 'practices', 'contractor', 'contractors', 'grader', 'graders',
    'linguist', 'linguists', 'author', 'authors', 'scribe', 'scribes', 'advocate', 'advocates',
    'banking', 'speaker', 'speakers', 'training', 'provider', 'providers', 'academic',
    'academics', 'pricing', 'collection', 'collections', 'radiology', 'teleoperator',
    'teleoperators', 'capability', 'capabilities',
    # tech/dev/ML tool & framework names (not companies) -- skill mentions, not identity
    'pytorch', 'tensorflow', 'jax', 'gymnasium', 'streamlit', 'dify', 'looker', 'hubert',
    'speechbrain', 'espnet', 'openmmlab', 'kubernetes', 'docker', 'git', 'github', 'python',
    'sql', 'nosql', 'api', 'apis', 'sdk', 'sdks', 'gpu', 'gpus', 'cpu', 'cpus', 'llm', 'llms',
    'aks', 'netcore', 'net', 'dotnet', 'kusto', 'sccm', 'vscode', 'npm', 'react', 'angular',
    'windows', 'macos', 'linux', 'unix', 'oauth', 'saml', 'ldap', 'dns', 'vpn', 'ssl', 'tls',
    'http', 'https', 'rest', 'soap', 'graphql', 'grpc', 'json', 'xml', 'yaml', 'csv', 'crm',
    'erp', 'sap', 'system', 'systems', 'framework', 'frameworks', 'library', 'libraries',
    'toolkit', 'toolkits', 'model', 'models', 'algorithm', 'algorithms', 'pipeline', 'pipelines',
    'database', 'databases', 'db', 'dbs', 'server', 'servers', 'cluster', 'clusters', 'network',
    'networks', 'app', 'apps', 'application', 'applications', 'tool', 'tools', 'software',
    'hardware', 'stack',
    # evaluative-commentary sentence-starters and generic acronyms (confirmed on
    # ResumeRating_Job_Candidate.Comment: GLiNER mislabels ordinary HR-evaluator phrasing as a
    # person/org, e.g. 'Highly overqualified' -> person, 'Good ML/DL foundations' -> org, 'Solid
    # Workday' -> org (16x) -- none of these are a real person or company identity, they're just
    # how an evaluator opens a sentence about the CANDIDATE's fit). 'good'/'strong'/'solid'/
    # 'highly' are common enough that adding them costs essentially no recall on real names
    # (a real name is never literally the word 'Highly'), while fixing a systemic, high-volume
    # false-positive source.
    'good', 'strong', 'solid', 'highly', 'lean', 'overqualified', 'foundation', 'foundations',
    'jd', 'ta', 'sde', 'hrbp', 'bpo', 'mlops', 'llmops', 'rlhf', 'rag', 'ngo', 'ngos', 'ae', 'md',
    'physician', 'physicians', 'supervisor', 'supervisors', 'freshman', 'freshmen', 'profile',
    'profiles', 'telco', 'saas', 'startup', 'startups', 'regex', 'fortune', 'pmp', 'vision',
    'nist', 'cdc',
    # procurement / purchase-order / contract generic vocabulary (confirmed on
    # PurchaseOrderLine.MCRDROPSHIPCOMMENT: GLiNER mislabels ordinary PO-comment phrasing as an
    # org, e.g. 'Old PO' -> org, 'Non-IT Subscription' -> org, 'tax dd consulting' -> org, none
    # of which name a real person or company -- they're just how a buyer/AP clerk annotates a
    # line item. Same precision-over-recall tradeoff already applied above for résumé/HR text.
    'po', 'pos', 'old', 'non', 'subscription', 'subscriptions', 'agreement', 'agreements',
    'contract', 'contracts', 'consulting', 'filing', 'filings', 'wave', 'waves', 'change',
    'changes', 'request', 'requests', 'accessory', 'accessories', 'statutory', 'representation',
    'representations', 'renewal', 'renewals', 'quote', 'quotes', 'order', 'orders', 'shipment',
    'shipments', 'fee', 'fees', 'rate', 'rates', 'monthly', 'annual', 'quarterly', 'yearly',
    'license', 'licenses', 'licence', 'licences', 'extension', 'extensions', 'registration',
    'registrations', 'forecast', 'cost', 'flow', 'dd',
}

# ── well-known brand/platform names ──────────────────────────────────────────────────────────
# Major, widely-recognized companies/platforms mentioned purely as a SKILL or employer
# reference (e.g. "no direct Workday HCM background", "NVIDIA GPU experience") -- not the
# candidate's own identity. Per explicit original user decision for
# ResumeRating_Job_Candidate: these stay real in that table (masking them provides no privacy
# benefit and corrupts evaluative text with a fake company name), while smaller/specific
# employers not on this list still get anonymized normally.
#
# IMPORTANT -- as of the domain-scoping fix, this list is ONLY consulted when
# table_domain(table) == 'resume' (see scrub_post's `domain` parameter in obi_anonymizer.py).
# In every other table (dyncrm_activity, dyncrm_leads, crm_itticket, etc.) these same brand
# names are real client/competitor identities and MUST be anonymized like any other company --
# do not add tables here expecting brands to be exempted; add them to RESUME_DOMAIN_TABLES
# above only if the table is genuinely resume/job-matching evaluative text.
KNOWN_BRAND_STOP = {
    'nvidia', 'azure', 'microsoft', 'aws', 'amazon', 'google', 'salesforce', 'workday',
    'servicenow', 'oracle', 'linkedin', 'youtube', 'databricks', 'stripe', 'samsung', 'siemens',
    'cohere', 'genai', 'vision ai', 'power bi', 'powerbi', 'power automate', 'ms365', 'o365',
    'gcp', 'sap', 'vmware', 'terraform', 'jira', 'confluence', 'coursera', 'udemy', 'zapier',
    'twilio', 'genesys', 'symantec', 'verisign', 'capgemini', 'jp morgan chase',
    'johnson & johnson', 'goldman sachs', 'cvs health', 'at&t', 'citibank', 'bny mellon',
    'first republic bank', 'mckesson', 'qualcomm', 'mediatek', 'intel', 'fedex', 'ikea',
    'marriott', 'netsuite', 'cognizant', 'wipro', 'airbus', 'rockwell automation',
    'analog devices', 'robert bosch', 'mercedes-benz', 'audi', 'porsche', 'hubspot', 'upwork',
    'prometheus', 'qualys', 'tenable', 'milliporesigma', 'synaptics', 'viewsonic',
    'acer america', 'cisco', 'apple', 'ibm', 'ringdna', 'zoominfo', 'ncr', 'honda', 'verizon',
    'adobe', 'shein', 'lumen technologies', 'chargeguard', 'creamistry', 'elastic',
    'oneforma', 'appen', 'scale ai', 'welocalize', 'rws moravia', 'transperfect', 'per scholas',
    'great learning', 'imbue ai', 'snorkel ai', 'outlier ai', 'handshake ai', 'figure ai',
    'dell', 'hp', 'lg', 'magnificent seven', 'cosmos/scope', 'isaac sim', 'edge ai',
    'fedramp', 'fp&a',
}

_PRONOUN_STOP = {'i', 'you', 'me', 'we', 'us', 'he', 'she', 'they', 'him', 'her', 'them',
                 'my', 'your', 'our', 'his', 'their', 'myself', 'yourself'}

# ── forced substitution rule (existing Centific/Pactera mechanism, unchanged) ────────────────
FORCED_MAP = {
    'centific': 'aventraa',
    'pactera':  'eventraa',
    'pacteraedge': 'eventraaedge',
    'uber':'Sanchez-Harris'
}

# ── manually curated company overrides (fix D's ONLY input -- see below) ────────────────────
# Add real company names here as you identify ones you want a guaranteed, deterministic fake
# for in free text (dyncrm_activity/dyncrm_leads/etc.), regardless of what GLiNER detects.
# Same reliability contract as FORCED_MAP above (Centific -> Aventraa): once a company is
# listed here, it ALWAYS gets faked to the value you give it, every time, everywhere in
# free text. Kept as a SEPARATE dict from FORCED_MAP (not merged into it) because FORCED_MAP's
# matching has NO word-boundary check by design -- safe only for distinctive made-up tokens
# like 'centific' that never collide with real words. A short/common company name (meta,
# apple, dell) WOULD collide with ordinary words and even HTML tag names (confirmed: 'meta'
# matched inside the literal `<meta http-equiv=...>` tag the first time this was tried) --
# entries here go through apply_literal_map()'s word-boundary + HTML-tag-safety guard instead.
#
# EARLIER VERSION of this dict was auto-populated from EVERY mapping_xref CompanyName row
# (93,695 of them) at runtime. Rolled back per explicit decision: that made the deterministic
# regex used to apply it recompile from a ~93K-way alternation on every single free-text cell
# (no caching), which turned a ~94min anonymization run into 3+ hours. This dict is now the
# ONLY source for the company backstop -- keep it to the specific companies you already know
# matter for a given slice; anything not listed here still gets normal GLiNER detection +
# the engine's existing (fast, per-value) mapping_xref reuse-first lookup, just without this
# extra deterministic guarantee.
MANUAL_COMPANY_MAP = {
    # 'ibm': 'Some Fixed Fake',

    # Found on PurchaseOrderLine.MCRDROPSHIPCOMMENT: left unchanged because each name contains
    # a pre-existing GENERIC_ENTITY_STOP word ('cloud'/'services'), which makes GLiNER's own
    # detection get discarded whole-span. Values below are each company's OWN existing,
    # already-established fake -- pulled from mapping_slice/mapping_xref (reused as-is, not
    # regenerated) so this stays consistent with every prior occurrence of the same company
    # elsewhere in the project.
    'alibaba': 'Kinetic Works Radian',                                    # mapping_slice id 5476
    'amazon web services': 'FakeCompany_00176',                      # mapping_xref id 50499
    'affinda': 'Meridian Data Cascade',                                   # mapping_xref id 813747
    'bounce marketing': 'Aurora Quantic',                                 # mapping_xref id 738335
    'olivine marketing llc': 'Aperture Pinnacle LLC',                     # mapping_slice id 270711
    'three marketeers commuincations group inc': 'Fathom Nexus Kinetic Group Inc',  # mapping_xref id 737456
    'whitesource software': 'Pinnacle Obsidian',                          # mapping_xref id 737429
    'mention solutions sas': 'Pinnacle Solutions SWS',                    # mapping_xref id 738323

    # Found on pwsdetail.QNRData/SummaryData: bare 4-letter client-code prefixes embedded in a
    # structured sub-field ("ContractClientRole":"CTFC-GEN-105"/"MSFT-CNE-216") -- too short and
    # context-free for GLiNER to catch reliably (same fragility as short state-code tokens
    # elsewhere in this file), so backstopped here rather than left to detection alone.
    'msft': 'ReedForgeEn LLC',                                            # mapping_xref id 182047 (already reused project-wide)
    'ctfc': 'dyzn',                                            # no prior mapping_xref entry -- newly curated
}

# Deterministic, pwsdetail-ONLY backstop (2026-08-03) -- every value below already has an
# established fake in mapping_xref, confirmed leaking verbatim (0% GLiNER detection) in a full,
# untruncated scan of pwsdetail's five JSON freetext columns (ExpenseData/QNRData/RevenueData/
# RiskData/SummaryData) against a lokes_verify-style corpus check. GLiNER (a natural-language NER
# model) has near-zero recall on these: they're bare internal codes (ResourceCCC cost-center IDs,
# "XX T<n>" location-tier tags, FCST_* expense category labels) with no sentence-like context, not
# the kind of thing an NER model was trained to recognize. A handful of plain person names that
# should have been caught by the normal path are included too (root cause for those specifically:
# the GENERIC_ENTITY_STOP suppression rule discarding a whole GLiNER span that happens to include
# an adjacent hyphen-glued generic word, e.g. "Student Worker-Jinsong Li" -- see the scrub_post fix
# alongside this dict; these entries are a belt-and-suspenders backstop for THIS confirmed set,
# not a substitute for that fix, since future new names won't be in this static list).
#
# IMPORTANT: apply this dict with apply_literal_map(..., case_adapt=False) — case_like() would
# corrupt these (it title-cases every alpha run of the fake to match the original's case pattern,
# which mangles deliberate acronym casing like 'C_TKH DN_Fathom' -> 'C_Tkh Dn_Fathom'). Confirmed
# empirically before adding this dict: 27 of these 55 fakes get mangled by case_like.
#
# Table-scoped deliberately (gate on table == 'pwsdetail' at the call site), unlike
# MANUAL_COMPANY_MAP above which applies project-wide -- these are short, generic-shaped tokens
# ("CN T1", "US T1") with a real, if small, risk of coincidentally matching unrelated text in a
# different table's freetext column, so the blast radius is kept to the one table they were
# confirmed on.
#
# Known pre-existing data-quality quirks in mapping_xref, carried through as-is (not introduced by
# this dict, not fixed here -- fixing them means picking a NEW fake, which is out of scope for a
# leak-closing backstop that must reuse each original's CANONICAL fake):
#   - 'us t1' and 'us t2' both already map to the same fake 'BROOKFIELD-ASHFORD' upstream in
#     mapping_xref -- the T1/T2 distinction is lost in the anonymized data. Pre-existing collision,
#     not created here.
#   - 'multimodal if preference data & rewrite' (a task/project name, not a person) already has a
#     garbled multi-word fake -- looks like an earlier GLiNER pass misread it as a compound name.
#     Reused as-is rather than re-curated, since the goal here is closing the leak, not auditing
#     mapping_xref's history.
PWSDETAIL_CODE_MAP = {
    # location-tier codes (ResourceLocation-adjacent, QNRData/SummaryData) -- by far the largest
    # share of the leak: these 5 alone were 18,462 of the 20,507 total leaked occurrences found.
    'cn t1': 'GREENVILLE-WESTBROOK',
    'cn t2': 'LAKEWOOD-FAIRVIEW',
    'in t1': 'CEDARVILLE-ELMWOOD',
    'us t1': 'BROOKFIELD-ASHFORD',
    'us t2': 'BROOKFIELD-ASHFORD',           # see note above: shares a fake with 'us t1' upstream

    # ResourceCCC cost-center / delivery-unit codes (QNRData/RevenueData)
    'c_edge_del cn_digital_expedia': 'O_HHVP_OCY UG_Fathom_Evercrest',
    'c_gdc cn_eng_emergingsh': 'K_SSJ BG_HMK_Indigo',
    'c_gdc cn_eng_isv': 'R_GRH HY_TEI_SOQ',
    'c_gdc cn_eng_ms': 'U_LEC HA_MJL_RY',
    'c_gdc cn_llmagenticai_ups': 'U_HLL UW_Cypress_QIH',
    'c_gdc cn_llmcoreai_ms': 'K_FLK PF_Radian_BD',
    'c_gdc cn_llmdata_dmc': 'F_XQI NT_Helios_SNI',
    'c_gdc eu_llmdata': 'R_MMX UL_Radian',
    'c_gdc eu_llmdata_amazon': 'D_CDP GA_Indigo_Helios',
    'c_gdc eu_llmhealthcare': 'C_TKH DN_Fathom',
    'c_gdc idc_eng_ms': 'R_KDR WHQ_NIN_VG',
    'c_gdc idc_llmcoreai': 'D_HJB QGV_Cascade',
    'c_gdc idc_llmhealthcare': 'C_GIV NTR_Monarch',
    'c_gdc idc_llmloc': 'R_ZQP EKH_Novena',
    'c_gdc sea_eng_ms': 'Z_GYO HFJ_NYZ_DY',
    'c_gdc sea_llmdata': 'Z_RAD GIF_Pinnacle',
    'c_gdc sea_llmdata_amazon': 'K_FRP NL_Solstice_Falconix',
    'c_gdc sea_llmdata_new': 'V_OWS JLH_Aperture_Blueridge',
    'c_gdc sea_llmloc_isaac': 'H_BLP QHJ_Polaris_Nimbus',
    'c_gdc sea_mlai': 'U_CCL KUZ_IVKB',
    'c_llm_data_us amazon': 'Q_CJA_Sapphire_MO Equinox',
    'c_llm_loc_us isaac': 'Y_NEX_Radian_DM CEDARPOINT',
    'gdc us_bu': 'NGJ CQ_KV',
    'llm_data_us amazon': 'KOC_Arcadia_SZ Emberline',

    # expense/task category labels (ExpenseData/RevenueData)
    'fcst exp_entertainment': 'DTXZ Monarch_Emberline',
    'fcst exp_others': 'XBPV Fathom_Kinetic',
    'fcst exp_travel expenses': 'GSYB Harborview_Vanguard Tessera',
    'translation pilot': 'Fathom Cirrus',
    'translation service': 'MapleReachO Group',
    'utterance generation': 'Titan xenon',
    'technical support': 'Cypress obsidian',
    'learning & development': 'Fathom & Solstice',
    'multimodal if preference data & rewrite': 'Saber MARIAMA Carymyrat Tsepho Fafo Dwaine',

    # apparent NER mistakes upstream in mapping_xref (public product name / generic org phrase
    # mapped to a person-shaped fake) -- reused as-is rather than re-curated, same rationale as
    # the note above.
    'azure devops': 'Brooke Harrison',
    'cypress automation': 'Kestrel Radian Sapphire',

    # genuine person names confirmed leaking raw in QNRData/ExpenseData/RevenueData -- missed by
    # the normal GLiNER + GENERIC_ENTITY_STOP path (see the scrub_post fix alongside this dict for
    # the "Student Worker-Name"/"C2C-Name" root cause). Backstopped here for the confirmed set;
    # the scrub_post fix is what should catch any NOT yet observed.
    'charles cooper': 'Cynthia Kouhia',
    'james smith': 'Russell Wright',
    'jie sheng': 'Xin Lin',
    'jing xiao': 'Anna Robinson',
    'jinsong li': 'Hannah Walker',
    'lei wang': 'Ava Torres',
    'lin gao': 'Carrie Chavez',
    'md abu sayed': 'Tony Payne',
    'meghna (idc)': 'Mouhedin (EZY)',
    'meghna travel': 'Vanguard Everline Blueridge',
    'qian wu': 'Wei Wang',
    'rong zhang': 'Genesis Garcia',
    'xinglei zong': 'Zoe Brown',
    'yabetse (us)': 'Luisianis (IN)',
    'ying he': 'Addison Garcia',
}
