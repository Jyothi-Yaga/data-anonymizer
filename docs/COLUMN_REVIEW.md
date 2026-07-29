# OBI slice — column anonymization review

> **What this file is:** the team's shared record of what got anonymized, as what type, per
> table — so that when more than one person works the same slice, nobody re-decides (or
> silently diverges from) a column a teammate already anonymized. **Before you `run` a table
> that already has a section below, use that section's decisions — don't re-analyze and
> re-decide independently.** For a table that ISN'T here yet, start from
> [COLUMN_REVIEW_TEMPLATE.md](COLUMN_REVIEW_TEMPLATE.md), get the team's sign-off, then add a
> section here (or copy the filled-in template into this file) before running.

**KEEP?** y = anonymized, n = left raw. **TYPE**/mode = as actually applied (reflects all
round-1 & round-2 corrections). `dyncrm_applicationuser` excluded (no PII).
`raw_json` is anonymized (freetext) in outlook_email, sharepoint_files & sharepoint_sites
(used by another team — kept in the schema and scrubbed, not dropped).

**`mode`** decides the code path: `structured` dispatches on `TYPE` below; `freetext` runs the
whole cell through GLiNER (detects embedded person/organization/email/phone-number spans) —
for a `freetext`-mode row, the `TYPE` column is not itself consulted.

**TYPE** (meaningful when `mode=structured`): `person | org | email | phone | id | url | birth |
region | country | location | amount | skip`. (`freetext` in the TYPE column below is a
carry-over label the tooling writes by convention for freetext-mode rows — it is a `mode`, not
one of the structured types above.)

> ⚠️ **Staleness note:** several rows below (e.g. `stepname`/`*stagename`/`*categoryname` typed
> `person`, ID-shaped columns typed `phone`, city/state columns typed `org`) reflect **what this
> specific run actually applied at the time**, before later engine fixes — the reference/enum
> deny-list that now auto-suggests `skip` for exactly those columns, and the dedicated
> `country`/`location`/`region`/`birth` types, didn't exist yet for the earliest entries here.
> This file is a historical record of what was applied, not a guarantee that re-running
> `analyze` on the same table today would suggest the same types. If you need current
> classification, re-run `analyze` and compare before trusting an old row verbatim.


## dyncrm_contact  — rows=47,830, 9 ON

| KEEP? | column | TYPE | mode | conf | sample |
|---|---|---|---|---|---|
| **y** | emailaddress1 | email | structured | 1.0 | aalleexx@amazon.com |
| **y** | firstname | person | structured | 0.7 | Alex |
| **y** | fullname | person | structured | 0.7 | Alex Reyes |
| **y** | lastname | person | structured | 0.7 | Reyes |
| **y** | yomifullname | person | structured | 0.7 | Alex Reyes |
| **y** | address1_city | org | structured | 0.5 | San Diego |
| **y** | address1_composite | org | structured | 0.5 | San Diego, California |
| **y** | jobtitle | person | structured | 0.5 | Tools Support Engineer |
| **y** | address1_stateorprovince | org | structured | 0.0 | California |

## VNSTeamMemberStaging  — rows=26,849, 10 ON

| KEEP? | column | TYPE | mode | conf | sample |
|---|---|---|---|---|---|
| **y** | RESOURCECOMPANYID | org | structured | 0.7 | vtus |
| **y** | RESOURCEID | id | structured | 0.7 | P0059234 |
| **y** | RESOURCENAME | person | structured | 0.7 | Rajnikanth Vydyula |
| **y** | ROLENAME | person | structured | 0.7 | Database Administrator-US T1 |
| **y** | LEGALENTITY | org | structured | 0.6 | vtus |
| **y** | ROLE | person | structured | 0.5 | TC00014-US T1 |
| **y** | EXECUTIONID | freetext | freetext | 0.4 | CentificExportTeamMembers2DLNew-2026-06-17T09:00:07-BCB221A5 |
| **y** | DEFINITIONGROUP | org | structured | 0.0 | CentificExportTeamMembers2DLNew |
| **y** | PROJID | id | structured | 0.0 | VTUSMSCOZ24P044 |
| **y** | VNSPONUMBER | id | structured | 0.0 | VTUS_PO000093 |
| n | PARTITION | org | structured | 0.0 | initial |

## dyncrm_leads  — rows=18,589, 14 ON

| KEEP? | column | TYPE | mode | conf | sample |
|---|---|---|---|---|---|
| **y** | emailaddress1 | email | structured | 1.0 | boyu.zhang@tiktok.com |
| **y** | address1_name | person | structured | 0.7 | London, England, United Kingdom |
| **y** | companyname | person | structured | 0.7 | Snorkel AI |
| **y** | firstname | person | structured | 0.7 | STANLEY |
| **y** | fullname | person | structured | 0.7 | STANLEY EVERAGE JR. |
| **y** | lastname | person | structured | 0.7 | EVERAGE JR. |
| **y** | new_leadsourcedetails | person | structured | 0.7 | Internet Research |
| **y** | yomifullname | person | structured | 0.7 | STANLEY EVERAGE JR. |
| **y** | exchangerate | amount | structured | 0.5 | 1.000000000000000000 |
| **y** | jobtitle | person | structured | 0.5 | Aviation AI Contributor |
| **y** | new_domain1 | url | structured | 0.5 | tiktok.com |
| **y** | address1_composite | org | structured | 0.0 | United States |
| **y** | address1_country | org | structured | 0.0 | United States |
| **y** | description | freetext | freetext | 0.0 | Invitation sent |

## dyncrm_systemuser  — rows=7,911, 21 ON

| KEEP? | column | TYPE | mode | conf | sample |
|---|---|---|---|---|---|
| **y** | address1_telephone1 | phone | structured | 1.0 | 4256589000 |
| **y** | domainname | email | structured | 1.0 | v_en_annotator105@pacteraedge.com |
| **y** | internalemailaddress | email | structured | 1.0 | v_en_annotator105@pacteraedge.com |
| **y** | mobilephone | phone | structured | 1.0 | +919390464157 |
| **y** | new_sbuheademail | email | structured | 1.0 | 2bcce6b9f5104f12ad64bf59c73aac0ashiva.jayaraman@centific.com |
| **y** | windowsliveid | email | structured | 1.0 | v_en_annotator105@pacteraedge.com |
| **y** | defaultodbfoldername | person | structured | 0.7 | Dynamics365 |
| **y** | firstname | person | structured | 0.7 | Portia |
| **y** | fullname | person | structured | 0.7 | Portia Baysa |
| **y** | lastname | person | structured | 0.7 | Baysa |
| **y** | yomifullname | person | structured | 0.7 | Portia Baysa |
| **y** | address1_city | org | structured | 0.5 | SINGAPORE |
| **y** | address1_composite | org | structured | 0.5 | SINGAPORE  SINGAPORE |
| **y** | exchangerate | amount | structured | 0.5 |  |
| **y** | title | person | structured | 0.5 | Annotator  |
| **y** | address1_country | org | structured | 0.0 | SINGAPORE |
| **y** | address1_line1 | org | structured | 0.0 | 14980 NE 31st Way Suite 100 |
| **y** | address1_postalcode | phone | structured | 0.0 | 98052 |
| **y** | address1_stateorprovince | org | structured | 0.0 | WA |
| **y** | userpuid | id | structured | 0.0 | 100320017DFCFBC0 |
| **y** | applicationiduri | id | structured | 0.3 | 629d6aaf-fc7b-4a58-b4fb-2694167421c9 |
| n | cr08f_sbuheadexist | org | structured | 0.0 | Yes |

## dyncrm_opportunity  — rows=5,905, 42 ON

| KEEP? | column | TYPE | mode | conf | sample |
|---|---|---|---|---|---|
| **y** | new_oppid | phone | structured | 1.0 | 202009-000216499 |
| **y** | customerneed | freetext | freetext | 0.95 | Lean on Centific's labeling team for a large portion of proj |
| **y** | description | freetext | freetext | 0.95 | MS FIN team need a portal to track and demonstrate tech tale |
| **y** | new_businesschallenge | freetext | freetext | 0.95 | End of April deadline to release a few product features. Lim |
| **y** | new_opportunityidcustom | phone | structured | 0.88 | 202201-E00001864 |
| **y** | name | person | structured | 0.7 | MS FIN Talent Insight Portal |
| **y** | new_p_fin_ext_pri_template_name | person | structured | 0.7 | 5.28_Steelcase_CentificWorkOrder.docx |
| **y** | new_p_fin_int_pri_file_name | person | structured | 0.7 | 5.20 - Steelcase Pricing calculator.xlsx |
| **y** | new_p_ppt_exec_summary_name | person | structured | 0.7 | 5.28_Steelcase_CentificWorkOrder.docx |
| **y** | new_p_sow_name | person | structured | 0.7 | 5.28_Steelcase_CentificWorkOrder.docx |
| **y** | new_stagename | person | structured | 0.7 | Confirm |
| **y** | stepname | person | structured | 0.7 | 1-Qualify |
| **y** | new_notifyemailstatus | email | structured | 0.6 | Send |
| **y** | actualvalue | amount | structured | 0.5 | 72000.000000000000000000 |
| **y** | actualvalue_base | amount | structured | 0.5 | 11328.060000000000000000 |
| **y** | budgetamount | amount | structured | 0.5 | 55000.000000000000000000 |
| **y** | budgetamount_base | amount | structured | 0.5 | 55000.000000000000000000 |
| **y** | estimatedvalue | amount | structured | 0.5 | 72000.000000000000000000 |
| **y** | estimatedvalue_base | amount | structured | 0.5 | 11328.060000000000000000 |
| **y** | exchangerate | amount | structured | 0.5 | 6.355900000000000000 |
| **y** | new_amountusd | amount | structured | 0.5 | 100000.000000000000000000 |
| **y** | new_nextstep | org | structured | 0.5 | Call with customer on 19th June |
| **y** | new_sbu | org | structured | 0.5 | SBU1 |
| **y** | new_sourcedetails | person | structured | 0.5 | Human Signal |
| **y** | proposedsolution | org | structured | 0.5 | MTPE |
| **y** | totalamount | amount | structured | 0.5 | 0E-18 |
| **y** | totalamount_base | amount | structured | 0.5 | 0E-18 |
| **y** | totalamountlessfreight | amount | structured | 0.5 | 0E-18 |
| **y** | totalamountlessfreight_base | amount | structured | 0.5 | 0E-18 |
| **y** | totaldiscountamount | amount | structured | 0.5 | 0E-18 |
| **y** | totaldiscountamount_base | amount | structured | 0.5 | 0E-18 |
| **y** | totallineitemamount | amount | structured | 0.5 | 0E-18 |
| **y** | totallineitemamount_base | amount | structured | 0.5 | 0E-18 |
| **y** | totallineitemdiscountamount | amount | structured | 0.5 | 0E-18 |
| **y** | totallineitemdiscountamount_base | amount | structured | 0.5 | 0E-18 |
| **y** | totaltax | amount | structured | 0.5 | 0E-18 |
| **y** | totaltax_base | amount | structured | 0.5 | 0E-18 |
| **y** | new_external_id__c | id | structured | 0.0 | 0066F00000mhigwQAA |
| **y** | new_p_final_id | id | structured | 0.0 | P-202502-E00005335-V1/F |
| **y** | new_p_id | id | structured | 0.0 | P-202502-E00005335-V1 |
| **y** | new_salesforcerecid | id | structured | 0.0 | 0066F00000mhigwQAA |
| **y** | new_sfdcacctid | id | structured | 0.0 | 0015000000NZAJ4AAP |
| n | skippricecalculation | amount | structured | 0.5 | 0 |
| n | new_itnote | org | structured | 0.0 | flag |
| n | new_p_project_duration | org | structured | 0.0 | 12 month |

## dyncrm_orders  — rows=4,740, 24 ON

| KEEP? | column | TYPE | mode | conf | sample |
|---|---|---|---|---|---|
| **y** | name | id | structured | 0.7 | 202110-E00001663 |
| **y** | new_sowid | phone | structured | 0.6 | 6000563175 |
| **y** | exchangerate | amount | structured | 0.5 | 1.000000000000000000 |
| **y** | new_biztaxrate | amount | structured | 0.5 | 0E-18 |
| **y** | new_orderamount | amount | structured | 0.5 | 719.440000000000000000 |
| **y** | new_orderamount_base | amount | structured | 0.5 | 719.440000000000000000 |
| **y** | new_vatrate | amount | structured | 0.5 | 0E-18 |
| **y** | skippricecalculation | amount | structured | 0.5 | 0 |
| **y** | totalamount | amount | structured | 0.5 | 0E-18 |
| **y** | totalamount_base | amount | structured | 0.5 | 0E-18 |
| **y** | totalamountlessfreight | amount | structured | 0.5 | 0E-18 |
| **y** | totalamountlessfreight_base | amount | structured | 0.5 | 0E-18 |
| **y** | totaldiscountamount | amount | structured | 0.5 | 0E-18 |
| **y** | totaldiscountamount_base | amount | structured | 0.5 | 0E-18 |
| **y** | totallineitemamount | amount | structured | 0.5 | 0E-18 |
| **y** | totallineitemamount_base | amount | structured | 0.5 | 0E-18 |
| **y** | totallineitemdiscountamount | amount | structured | 0.5 | 0E-18 |
| **y** | totallineitemdiscountamount_base | amount | structured | 0.5 | 0E-18 |
| **y** | totaltax | amount | structured | 0.5 | 0E-18 |
| **y** | totaltax_base | amount | structured | 0.5 | 0E-18 |
| **y** | new_contractpo | org | structured | 0.0 | UWHS_10142022_1790 |
| **y** | new_orderyear | org | structured | 0.0 | 2022 |
| **y** | new_pid | id | structured | 0.0 | UWHSAPPLE21P018 |
| **y** | ordernumber | id | structured | 0.0 | ORD-50856-G8S2L1 |

## emp_mstr  — rows=2,033, 15 ON

| KEEP? | column | TYPE | mode | conf | sample |
|---|---|---|---|---|---|
| **y** | Email | email | structured | 1.0 | vivian.ho@centific.com |
| **y** | EmployeeID | id | structured | 0.7 | P0082454 |
| **y** | ManagerID | id | structured | 0.7 | P0152225 |
| **y** | ManagerName | person | structured | 0.7 | Ana Gallego Ezpeleta |
| **y** | NameEN | person | structured | 0.7 | Vivian Ho |
| **y** | Department | org | structured | 0.6 | C_LLM_Loc_ISAAC |
| **y** | LegalEntity | org | structured | 0.6 | Centific Global Solutions (SG) Pte. Ltd. |
| **y** | ContractLocation | org | structured | 0.5 | Singapore |
| **y** | L2 | org | structured | 0.5 | LLM |
| **y** | L3 | org | structured | 0.5 | LLM_Loc |
| **y** | L4 | org | structured | 0.5 | LLM_Loc_ISAAC |
| **y** | L5 | org | structured | 0.5 | LLM_Loc_ISAAC |
| **y** | WorkCountry_PWS | org | structured | 0.5 | SGP |
| **y** | WorkLocation | org | structured | 0.5 | Singapore Office |
| **y** | L1 | org | structured | 0.0 | EDGE |
| n | OnboardDate | phone | structured | 1.0 | 2016-11-28 |
| n | CostCategory | org | structured | 0.5 | COST |
| n | Type | person | structured | 0.5 | Employee |
| n | JobGrade | org | structured | 0.0 | P4 |

## dyncrm_account  — rows=2,008, 12 ON

| KEEP? | column | TYPE | mode | conf | sample |
|---|---|---|---|---|---|
| **y** | emailaddress1 | email | structured | 1.0 | Ann.Hirschey@tysonfoods.com |
| **y** | address1_primarycontactname | person | structured | 0.7 | Ann Hirschey |
| **y** | address2_primarycontactname | person | structured | 0.7 | Ann Hirschey |
| **y** | name | org | structured | 0.7 | Baidu Corp |
| **y** | new_accountnamebybu | person | structured | 0.7 | Baidu Corp SBU1 |
| **y** | new_dynamicsaccountid | phone | structured | 0.64 | 202109-0668 |
| **y** | address1_composite | org | structured | 0.5 | 400 S Jefferson St, Chicago, IL 60607 |
| **y** | address1_line1 | org | structured | 0.5 | 400 S Jefferson St, Chicago, IL 60607 |
| **y** | address2_composite | org | structured | 0.5 | 400 S Jefferson St, Chicago, IL 60607 |
| **y** | address2_line1 | org | structured | 0.5 | 400 S Jefferson St, Chicago, IL 60607 |
| **y** | exchangerate | amount | structured | 0.5 | 1.000000000000000000 |
| **y** | websiteurl | url | structured | 0.3 | http://www.baidu.com/ |

## PurchaseOrderLine  — rows=1,955, 44 ON

| KEEP? | column | TYPE | mode | conf | sample |
|---|---|---|---|---|---|
| **y** | DELIVERYADDRESSLOCATIONID | phone | structured | 1.0 | 000000101 |
| **y** | DELIVERYADDRESSNAME | person | structured | 0.7 | CENTIFIC GLOBAL SOLUTIONS, INC |
| **y** | PROCUREMENTPRODUCTCATEGORYNAME | person | structured | 0.7 | Subcontractor Fee_CP |
| **y** | CALCULATELINEAMOUNT | amount | structured | 0.5 | 1 |
| **y** | DEFAULTLEDGERDIMENSIONDISPLAYVALUE | org | structured | 0.5 | -UWHSUPSOS23P032-5060000535-LLM_AgenticAI_DPT-Delivery_Digit |
| **y** | DEFINITIONGROUP | org | structured | 0.5 | PO Export |
| **y** | DELIVERYADDRESSDESCRIPTION | org | structured | 0.5 | CENTIFIC GLOBAL SOLUTIONS, INC |
| **y** | FIXEDPRICECHARGES | amount | structured | 0.5 | 0.000000 |
| **y** | GSTHSTTAXTYPE | amount | structured | 0.5 | 0 |
| **y** | INTRASTATSTATISTICVALUE | amount | structured | 0.5 | 0.000000 |
| **y** | ISTAX1099GTRADEORBUSINESSINCOME | amount | structured | 0.5 | 0 |
| **y** | ISTAX1099SPROPERTYORSERVICES | amount | structured | 0.5 | 0 |
| **y** | LINEAMOUNT | amount | structured | 0.5 | 0.000000 |
| **y** | LINEDESCRIPTION | org | structured | 0.5 | Subcontractor Fee |
| **y** | LINEDISCOUNTAMOUNT | amount | structured | 0.5 | 0.000000 |
| **y** | LINEDISCOUNTPERCENTAGE | amount | structured | 0.5 | 0.000000 |
| **y** | MCRDROPSHIPCOMMENT | freetext | freetext | 0.5 | Monthly Fee |
| **y** | MULTILINEDISCOUNTAMOUNT | amount | structured | 0.5 | 0.000000 |
| **y** | MULTILINEDISCOUNTPERCENTAGE | amount | structured | 0.5 | 0.000000 |
| **y** | ORDEREDCATCHWEIGHTQUANTITY | amount | structured | 0.5 | 0.000000 |
| **y** | ORDEREDPURCHASEQUANTITY | amount | structured | 0.5 | 0.000000 |
| **y** | OVERRIDESALESTAX | amount | structured | 0.5 | 0 |
| **y** | PROJECTSALESPRICE | amount | structured | 0.5 | 0.000000 |
| **y** | PURCHASEPRICE | amount | structured | 0.5 | 142.000000 |
| **y** | PURCHASEPRICEQUANTITY | amount | structured | 0.5 | 1.000000000000 |
| **y** | SKIPCREATEAUTOCHARGES | amount | structured | 0.5 | 1 |
| **y** | TAX1099AMOUNT | amount | structured | 0.5 | 0.000000 |
| **y** | TAX1099GSTATETAXWITHHELDAMOUNT | amount | structured | 0.5 | 0.000000 |
| **y** | TAX1099SBUYERPARTOFREALESTATETAXAMOUNT | amount | structured | 0.5 | 0.000000 |
| **y** | TAX1099STATEAMOUNT | amount | structured | 0.5 | 0.000000 |
| **y** | TAX1099TYPE | amount | structured | 0.5 | 0 |
| **y** | DATAAREAID | org | structured | 0.0 | UWHS |
| **y** | DELIVERYADDRESSCOUNTRYREGIONID | org | structured | 0.0 | USA |
| **y** | DELIVERYADDRESSCOUNTRYREGIONISOCODE | org | structured | 0.0 | US |
| **y** | DELIVERYADDRESSSTREET | org | structured | 0.0 | 14980 NE 31st Way, Suite 100,  Redmond, WA 98052 |
| **y** | DELIVERYADDRESSZIPCODE | phone | structured | 0.0 | 10019 |
| **y** | EXECUTIONID | id | structured | 0.0 | PO Export-2026-06-17T18:34:09-E628A7C746BA49A8BDCA35F31ED945 |
| **y** | FORMATTEDDELVERYADDRESS | org | structured | 0.0 | 14980 NE 31st Way, Suite 100,  Redmond, WA 98052 USA |
| **y** | INVENTORYLOTID | id | structured | 0.0 | UWHS-000001 |
| **y** | PROJECTID | id | structured | 0.0 | UWHSUPSOS23P032 |
| **y** | PURCHASEORDERNUMBER | id | structured | 0.0 | UWHS_PO000001 |
| **y** | PURCHASEREQUISITIONID | id | structured | 0.0 | PR000161 |
| **y** | REQUESTERPERSONNELNUMBER | id | structured | 0.0 | P0147332 |
| **y** | TAX1099BOXID | id | structured | 0.0 | NEC-01 |
| n | PARTITION | org | structured | 0.0 | initial |
| n | PROJECTCATEGORYID | skip | structured | 0.0 | Subcontractor Fee |
| n | PROJECTLINEPROPERTYID | skip | structured | 0.0 | Billable |
| n | PROJECTSALESCURRENCYCODE | skip | structured | 0.0 | USD |
| n | PROJECTSALESUNITSYMBOL | org | structured | 0.0 | Hour |
| n | PURCHASEUNITSYMBOL | org | structured | 0.0 | Hour |

## PurchaseOrderHeader  — rows=1,692, 22 ON

| KEEP? | column | TYPE | mode | conf | sample |
|---|---|---|---|---|---|
| **y** | DELIVERYADDRESSLOCATIONID | phone | structured | 1.0 | 000000101 |
| **y** | EMAIL | email | structured | 1.0 | richard.liu@consultils.com |
| **y** | DELIVERYADDRESSNAME | person | structured | 0.7 | CENTIFIC GLOBAL SOLUTIONS, INC |
| **y** | PAYMENTTERMSNAME | person | structured | 0.7 | 030 NET |
| **y** | PURCHASEORDERNAME | person | structured | 0.7 | 32 Donato Technologies Inc |
| **y** | VENDORPAYMENTMETHODNAME | person | structured | 0.7 | Wire |
| **y** | VNSCONSULTANTNAME | person | structured | 0.7 | Sinh Tran |
| **y** | INVOICEVENDORACCOUNTNUMBER | org | structured | 0.6 | UWHS_V000037 |
| **y** | ORDERVENDORACCOUNTNUMBER | org | structured | 0.6 | UWHS_V000037 |
| **y** | VENDORPOSTINGPROFILEID | org | structured | 0.6 | AP payment |
| **y** | AREPRICESINCLUDINGSALESTAX | amount | structured | 0.5 | 0 |
| **y** | ATTENTIONINFORMATION | org | structured | 0.5 | India Office - Hyderabad |
| **y** | CASHDISCOUNTPERCENTAGE | amount | structured | 0.5 | 0.000000 |
| **y** | DEFAULTLEDGERDIMENSIONDISPLAYVALUE | org | structured | 0.5 | -UWHSUPSOS23P032-5060000535-LLM_AgenticAI_DPT-Delivery_Digit |
| **y** | DELIVERYADDRESSDESCRIPTION | org | structured | 0.5 | CENTIFIC GLOBAL SOLUTIONS, INC |
| **y** | OVERRIDESALESTAX | amount | structured | 0.5 | 0 |
| **y** | REASONCOMMENT | org | structured | 0.5 | Project Centian |
| **y** | DELIVERYADDRESSSTREET | org | structured | 0.0 | 14980 NE 31st Way, Suite 100,  Redmond, WA 98052 |
| **y** | DELIVERYADDRESSZIPCODE | phone | structured | 0.0 | 10019 |
| **y** | FORMATTEDDELIVERYADDRESS | org | structured | 0.0 | 14980 NE 31st Way, Suite 100,  Redmond, WA 98052 USA |
| **y** | REQUESTERPERSONNELNUMBER | id | structured | 0.0 | P0147332 |
| **y** | VNSWORKLOCATION | org | structured | 0.0 | US |
| n | DEFINITIONGROUP | org | structured | 0.5 | PO Export |
| n | TOTALDISCOUNTPERCENTAGE | amount | structured | 0.5 | 0.000000 |
| n | CURRENCYCODE | skip | structured | 0.0 | USD |
| n | DATAAREAID | skip | structured | 0.0 | UWHS |
| n | DELIVERYADDRESSCOUNTRYREGIONID | skip | structured | 0.0 | USA |
| n | DELIVERYADDRESSCOUNTRYREGIONISOCODE | skip | structured | 0.0 | US |
| n | EXECUTIONID | skip | structured | 0.0 | PO Export-2026-06-17T18:34:09-E628A7C746BA49A8BDCA35F31ED945 |
| n | LANGUAGEID | skip | structured | 0.0 | en-US |
| n | ORDERERPERSONNELNUMBER | skip | structured | 0.0 | P0187282 |
| n | PARTITION | org | structured | 0.0 | initial |
| n | PROJECTID | skip | structured | 0.0 | UWHSUPSOS23P032 |
| n | PURCHASEORDERNUMBER | skip | structured | 0.0 | UWHS_PO000001 |
| n | VNSCONTRACTORMAN | org | structured | 0.0 | P0185258 |
| n | VNSSERVICEPROVIDED | org | structured | 0.0 | collaboration and technical workshops |

## dyncrm_activity  — rows=1,434, 3 ON

| KEEP? | column | TYPE | mode | conf | sample |
|---|---|---|---|---|---|
| **y** | description | freetext | freetext | 0.95 | <html lang="en"><head>  <meta http-equiv="Content-Type" cont |
| **y** | exchangerate | amount | structured | 0.5 | 6.993006990000000000 |
| **y** | subject | org | structured | 0.5 | Connection Request sent from Sales Navigator |
| n | activitytypecode | skip | structured | 0.0 | task |

## pwsheader  — rows=1,077, 25 ON

| KEEP? | column | TYPE | mode | conf | sample |
|---|---|---|---|---|---|
| **y** | PracticeandDeliveryUnit | freetext | freetext | 0.95 | [{"practice":"EDGE_Delivery","practice_name":"EDGE_Delivery" |
| **y** | Bidding_currency_name | person | structured | 0.7 | USD |
| **y** | Billing_type_name | freetext | freetext | 0.7 | Time and Material-Monthly |
| **y** | Customer_name | person | structured | 0.7 | Amazon AWS |
| **y** | DM_name | person | structured | 0.7 | Ruosen Liao |
| **y** | FocoBO_name | person | structured | 0.7 | Chen Emma |
| **y** | MainPM_name | person | structured | 0.7 | Ruosen Liao |
| **y** | Opportunity_name | person | structured | 0.7 | FY24_AMZN_AWS Transcribe_PII conversation_AI |
| **y** | Opportunity_salesname | person | structured | 0.7 | Rimi Endo |
| **y** | ProductLineName | person | structured | 0.7 | Globalization + Localization Services |
| **y** | Project_name | person | structured | 0.7 | test-emma |
| **y** | Project_service_type_name | person | structured | 0.7 | Project-based |
| **y** | Project_signing_entity_name | org | structured | 0.7 | UWHS_CENTIFIC GLOBAL SOLUTIONS, INC |
| **y** | Unit_price_type_name | person | structured | 0.7 | Monthly |
| **y** | ApplicationComments | org | structured | 0.5 | per sales, there will be no SOW, PO only |
| **y** | BiddingRate | amount | structured | 0.5 | 0.0 |
| **y** | Cash_discount | amount | structured | 0.5 | 0.0 |
| **y** | Client_discount | amount | structured | 0.5 | 0.0 |
| **y** | Inflation_rate | amount | structured | 0.5 |  |
| **y** | NetMarginValue | amount | structured | 0.5 | 0.0 |
| **y** | PlanningRate | amount | structured | 0.5 |  |
| **y** | Tax | amount | structured | 0.5 |  |
| **y** | View_total_cost | amount | structured | 0.5 | 0.0 |
| **y** | View_total_revenue | amount | structured | 0.5 |  |
| **y** | NetMarginDisplay | org | structured | 0.0 | 0.0% |
| n | Opportunity_businesssegment | org | structured | 0.5 | Enterprise AI |
| n | Opportunity_masterdivision | org | structured | 0.5 | MS Engineering |
| n | Opportunity_type | org | structured | 0.5 | Existing |
| n | ProductLine | org | structured | 0.5 | PGS |
| n | SBU | org | structured | 0.5 | SBU1 |
| n | Unit_price_type | amount | structured | 0.5 | 3 |
| n | Opportunity_id | skip | structured | 0.0 | 202405-E00004353 |
| n | PId | skip | structured | 0.0 | UWHSAWSSI24P093 |
| n | PWSID | skip | structured | 0.0 | PDP24050005 |
| n | ParentPWSID | skip | structured | 0.0 | MPDP24050002 |
| n | RelatedPDP | org | structured | 0.0 | PDP24060112 |
| n | RootPDP | org | structured | 0.0 | PDP24060112 |

## pwsdetail  — rows=1,077, 5 ON

| KEEP? | column | TYPE | mode | conf | sample |
|---|---|---|---|---|---|
| **y** | QNRData | freetext | freetext | 0.9 | {"form":{"TotalResourceCost":168,"GrossRevenue":0,"GrossMarg |
| **y** | ExpenseData | freetext | freetext | 0.6 | {"form":{"TotalCostExpense":0,"TotalBillableExpense":0,"Mark |
| **y** | RevenueData | freetext | freetext | 0.45 | {"form":{"GrossRevenue":0,"GrossMarginD":0,"TotalSubCost":0, |
| **y** | RiskData | freetext | freetext | 0.4 | {"form":{"comment":""},"details":[{"type":null,"type_name":" |
| **y** | SummaryData | freetext | freetext | 0.4 | {"form":{"NetRevenue":0,"TotalCost":168,"NetMarginPer":0,"Cl |
| n | PWSID | skip | structured | 0.0 | PDP24050005 |

## project_stage  — rows=721, 21 ON

| KEEP? | column | TYPE | mode | conf | sample |
|---|---|---|---|---|---|
| **y** | Email | email | structured | 1.0 | shustera@amazon.com |
| **y** | LOCATIONID | phone | structured | 1.0 | 000084804 |
| **y** | ccc | phone | structured | 1.0 | 5060000836 |
| **y** | EXECUTIONID | freetext | freetext | 0.95 | Export across company: IDBF - 3/25/2026 03:01:21 am.-2DEE85C |
| **y** | DELIVERYNAME | person | structured | 0.7 | No. 26/1, Brigade Gateway, World Trade Centre, 10th Floor, D |
| **y** | PROJECTNAME | person | structured | 0.7 | FY24_AMZN_WWS_Alexa Shopping_Critical Data Yellow Badge_LLM |
| **y** | VNSACCOUNTMANAGERPERSONNELNUMBER | id | structured | 0.7 | P0114360 |
| **y** | VNSDELIVERYMANAGERPERSONNELNUMBER | id | structured | 0.7 | P0049522 |
| **y** | CUSTOMERACCOUNT | org | structured | 0.6 | IDBF_C000006 |
| **y** | DEFAULTINVOICEACCOUNT | org | structured | 0.6 | 65 Amazon Seller Services Private Limited |
| **y** | CANVERIFYCOSTAGAINSTREMAININGFORECAST | amount | structured | 0.5 | 0 |
| **y** | INVOICECOST | amount | structured | 0.5 | 0 |
| **y** | VNSCLIENTPRODUCTTEAM | org | structured | 0.5 | Amazon D2AS |
| **y** | VNSMASTERDIVISION | org | structured | 0.5 | Amazon-Alexa Shoppin |
| **y** | ZAKATPROJECTVALUE | amount | structured | 0.5 | 0.000000 |
| **y** | DIMENSIONDISPLAYVALUE | freetext | freetext | 0.0 | -IDBFADCPL24P002-5060000836-LLM_Data_Amazon-LLM_Data-LLM-COS |
| **y** | VNSBOPERSONNELNUMBER | id | structured | 0.0 | P0190706 |
| **y** | VNSMAINPMPERSONNELNUMBER | id | structured | 0.0 | P0000871 |
| **y** | WORKERRESPFINANCIALPERSONNELNUMBER | id | structured | 0.0 | P0001164 |
| **y** | WORKERRESPONSIBLEPERSONNELNUMBER | id | structured | 0.0 | P3002672 |
| **y** | WORKERRESPSALESPERSONNELNUMBER | id | structured | 0.0 | P0189268 |
| n | C3 | org | structured | 0.5 | LLM_Data_Amazon |
| n | DEFINITIONGROUP | org | structured | 0.5 | ExportProjectIncSyncDL30min |
| n | ProjectGroup | org | structured | 0.5 | T&M-ITEM |
| n | TOTALEFFORTINHOURS | amount | structured | 0.5 | 117464.651513 |
| n | CALENDAR | org | structured | 0.0 | 5D40 |
| n | DATAAREAID | skip | structured | 0.0 | IDBF |
| n | JOBIDENTIFICATION | org | structured | 0.0 | IDBF-001709 |
| n | PARENTPROJECT | org | structured | 0.0 | IDBFMIRPL24P002 |
| n | PARTITION | org | structured | 0.0 | initial |
| n | PROJECTCONTRACTID | skip | structured | 0.0 | IDBF_PCID000053 |
| n | ProjectId | skip | structured | 0.0 | IDBFADCPL24P002 |
| n | SALESTAXGROUP | org | structured | 0.0 | IGST18 |
| n | SUBPROJECTIDFORMAT | org | structured | 0.0 | _## |
| n | VNSBILLINGCURRENCY | org | structured | 0.0 | INR |
| n | VNSCUSTCLASSIFICATIONID | skip | structured | 0.0 | Amazon WW Stores |
| n | VNSINVOICETYPE | org | structured | 0.0 | Proforma Invoice |
| n | VNSOPPORTUNITYID | skip | structured | 0.0 | 202406-E00004443 |
| n | VNSPDPID | skip | structured | 0.0 | PDP24060238 |

## outlook_email  — rows=206, 12 ON

| KEEP? | column | TYPE | mode | conf | sample |
|---|---|---|---|---|---|
| **y** | file_path | freetext | freetext | 1.0 | raff.ripoll@centific.com/emails/emails_batch_013.json |
| **y** | from_address | email | structured | 1.0 | kiran.mallakunta@centific.com |
| **y** | folder_name | email | structured | 0.98 | raff.ripoll@centific.com |
| **y** | cc_recipients | freetext | freetext | 0.95 | [{"emailAddress": {"name": "Akshat Mandloi", "address": "aks |
| **y** | to_recipients | freetext | freetext | 0.95 | [{"emailAddress": {"name": "Venkat Rangapuram", "address": " |
| **y** | from_name | person | structured | 0.7 | Kiran Mallakunta |
| **y** | attachment_index | freetext | freetext | 0.55 | [{"userUpn": "raff.ripoll@centific.com", "messageId": "AAMkA |
| **y** | body_content | freetext | freetext | 0.55 | <html><head>  <meta http-equiv="Content-Type" content="text/ |
| **y** | body_preview | freetext | freetext | 0.55 | Kiran, great work on the report. One ask — can we add the Vi |
| **y** | subject | freetext | freetext | 0.5 | Weekly Proposal Highlights \|\| Week ending 22 May 2026 |
| **y** | raw_json | freetext | freetext | 0.4 | {"@odata.etag": "W/\"CQAAABYAAAB88Bf4+WTpS4qlTOSyriIJAAfwZnv |
| **y** | bcc_recipients | freetext | freetext | 0.0 | [] |
| n | id | freetext | freetext | 0.4 | AAMkAGY3ZDEzOWRjLTlkZWItNDhmYy1hZDJmLTY3YzFjMTQzMjI2MQBGAAAA |
| n | attachments | org | structured | 0.0 | [] |
| n | body_content_type | org | structured | 0.0 | html |
| n | categories | org | structured | 0.0 | [] |
| n | importance | org | structured | 0.0 | normal |
| n | odata_etag | org | structured | 0.0 | W/"CQAAABYAAAB88Bf4+WTpS4qlTOSyriIJAAfwZnvR" |
| n | reply_to | org | structured | 0.0 | [] |
| n | source_file | org | structured | 0.0 | emails_batch_013.json |

## VendorInvoiceLine  — rows=153, 24 ON

| KEEP? | column | TYPE | mode | conf | sample |
|---|---|---|---|---|---|
| **y** | DELIVERYNAME | person | structured | 0.7 | Centific |
| **y** | PROCUREMENTCATEGORYHIERARCHYNAME | person | structured | 0.7 | Centific Procurement Categories |
| **y** | PROCUREMENTCATEGORYNAME | person | structured | 0.7 | Subscription Purchase |
| **y** | INVOICEACCOUNT | org | structured | 0.6 | UWHS_V000145 |
| **y** | VENDORACCOUNT | org | structured | 0.6 | UWHS_V000145 |
| **y** | VNSPROJECTCOMPANY | org | structured | 0.6 | uwhs |
| **y** | ADJUSTEDUNITPRICE | amount | structured | 0.5 | 0.000000 |
| **y** | AMOUNT | amount | structured | 0.5 | 0.000000 |
| **y** | DISCOUNT | amount | structured | 0.5 | 0.000000 |
| **y** | LINEDESCRIPTION | org | structured | 0.5 | Consulting Service |
| **y** | MULTILINEDISCOUNT | amount | structured | 0.5 | 0.000000 |
| **y** | NETAMOUNT | amount | structured | 0.5 | 10947.810000 |
| **y** | PRICEUNIT | amount | structured | 0.5 | 1.000000000000 |
| **y** | RELEASEALLRETAINEDAMOUNT | amount | structured | 0.5 | 0 |
| **y** | RETAINAGEAMOUNT | amount | structured | 0.5 | 0.000000 |
| **y** | TAX1099AMOUNT | amount | structured | 0.5 | 0.000000 |
| **y** | TAX1099GSTATETAXWITHHELDAMOUNT | amount | structured | 0.5 | 0.000000 |
| **y** | TAX1099SBUYERPARTOFREALESTATETAXAMOUNT | amount | structured | 0.5 | 0.000000 |
| **y** | TAX1099STATEAMOUNT | amount | structured | 0.5 | 0.000000 |
| **y** | TOTALRETAINEDAMOUNT | amount | structured | 0.5 | 0.000000 |
| **y** | UNITPRICE | amount | structured | 0.5 | 2.190000 |
| **y** | VNSPROJSALESPRICE | amount | structured | 0.5 | 0.000000 |
| **y** | HEADERREFERENCE | org | structured | 0.0 | UWHS-014207 |
| **y** | PURCHASEORDER | org | structured | 0.0 | UWHS_PO000147 |
| n | CHANGEQUANTITYMANUALLY | amount | structured | 0.5 | 0 |
| n | CHARGESONPURCHASES | amount | structured | 0.5 | 0.000000 |
| n | CWREMAININGQUANTITY | amount | structured | 0.5 | 0.000000 |
| n | DIMENSIONDISPLAYVALUE | org | structured | 0.5 | --6060000547-COO_BO_DPT-COO_BO-COO-G&A----- |
| n | DISCOUNTPERCENT | amount | structured | 0.5 | 0.000000 |
| n | ISTAX1099GTRADEORBUSINESSINCOME | amount | structured | 0.5 | 0 |
| n | ISTAX1099SPROPERTYORSERVICES | amount | structured | 0.5 | 0 |
| n | MULTILINEDISCOUNTPERCENTAGE | amount | structured | 0.5 | 0.000000 |
| n | OVERRIDESALESTAX | amount | structured | 0.5 | 0 |
| n | RETAINPERCENTAGE | amount | structured | 0.5 | 0.000000 |
| n | TAX1099BOX | org | structured | 0.5 | MISC-01 |
| n | TAX1099TYPE | amount | structured | 0.5 | 0 |
| n | CURRENCY | org | structured | 0.0 | USD |
| n | DATAAREAID | skip | structured | 0.0 | UWHS |
| n | DEFINITIONGROUP | org | structured | 0.0 | vendor invoice entity |
| n | DIMENSIONNUMBER | skip | structured | 0.0 | AllBlank |
| n | EXECUTIONID | skip | structured | 0.0 | vendor invoice entity-2026-06-18T04:57:36-5A8B07B2850C4BD395 |
| n | PARTITION | org | structured | 0.0 | initial |
| n | UNIT | org | structured | 0.0 | Piece |
| n | VNSPROJCATEGORYID | skip | structured | 0.0 | NPT-Expense |
| n | VNSPROJID | skip | structured | 0.0 | UWHSZZZZZ24I076 |
| n | VNSPROJLINEPROPERTYID | skip | structured | 0.0 | Billable |
| n | VNSPROJSALESCURRENCYID | skip | structured | 0.0 | USD |
| n | VNSPROJSALESUNIT | org | structured | 0.0 | Piece |

## VendorInvoiceSubLine  — rows=85, 2 ON

| KEEP? | column | TYPE | mode | conf | sample |
|---|---|---|---|---|---|
| **y** | PURCHASEQUANTITY | amount | structured | 0.5 | 1.000000 |
| **y** | PURCHASEORDER | org | structured | 0.0 | UWHS_PO000189 |
| n | DATAAREAID | skip | structured | 0.0 | UWHS |
| n | DEFINITIONGROUP | org | structured | 0.0 | vendor invoice entity |
| n | EXECUTIONID | skip | structured | 0.0 | vendor invoice entity-2026-06-18T04:57:36-5A8B07B2850C4BD395 |
| n | INVOICELINEREFERENCE | org | structured | 0.0 | UWHS-014514 |
| n | PARTITION | org | structured | 0.0 | initial |
| n | PRODUCTRECEIPTNUMBER | skip | structured | 0.0 |  113H-PJ9M-DR1F |

## dyncrm_competitor  — rows=84, 2 ON

| KEEP? | column | TYPE | mode | conf | sample |
|---|---|---|---|---|---|
| **y** | name | person | structured | 0.7 | Internal Resourcing |
| **y** | exchangerate | amount | structured | 0.5 | 1.000000000000000000 |

## sharepoint_files  — rows=62, 21 ON

| KEEP? | column | TYPE | mode | conf | sample |
|---|---|---|---|---|---|
| **y** | created_by_user_emails | email | structured | 1.0 | prithiviraj.pradeep@centific.com |
| **y** | folder_name | email | structured | 1.0 | raff.ripoll@centific.com |
| **y** | modified_by_user_emails | email | structured | 1.0 | prithiviraj.pradeep@centific.com |
| **y** | raw_json | freetext | freetext | 0.95 | {"@odata.etag": "\"{24AFDA04-5A2D-4B50-9484-07DBB03AB974},2\ |
| **y** | site_id | freetext | freetext | 0.95 | digitaltechedge.sharepoint.com,9d426a5c-0415-425d-88aa-2f255 |
| **y** | created_by_app_names | person | structured | 0.7 | Microsoft Teams |
| **y** | created_by_user_names | person | structured | 0.7 | Prithiviraj Pradeep |
| **y** | drive_name | person | structured | 0.7 | Documents |
| **y** | modified_by_app_names | person | structured | 0.7 | Microsoft Teams |
| **y** | modified_by_user_names | person | structured | 0.7 | Prithiviraj Pradeep |
| **y** | name | person | structured | 0.7 | DeepSeek To Release Next Flagship AI Model With Strong Codin |
| **y** | parent_folder_name | person | structured | 0.7 | General |
| **y** | site_name | person | structured | 0.7 | LLM Data |
| **y** | file_path | freetext | freetext | 0.5 | raff.ripoll@centific.com/sites/group_sites/LLM Data/files_in |
| **y** | downloaded_path | freetext | freetext | 0.3 | https://obistorageaccount.blob.core.windows.net/obi-project/ |
| **y** | modified_by_user_ids | id | structured | 0.3 | 5dbb24de-25a2-451b-af2a-6be077c485a6 |
| **y** | redownload_url | url | structured | 0.3 | https://graph.microsoft.com/v1.0/drives/b!XGpCnRUEXUKIqi8lVd |
| **y** | web_url | url | structured | 0.3 | https://digitaltechedge.sharepoint.com/sites/LLMData/Shared% |
| n | parent_folder_path | freetext | freetext | 0.4 | /drives/b!XGpCnRUEXUKIqi8lVdI648Fnboi29LJEqDqN_WR8YhJz7ub9PW |
| **y** | created_by_app_ids | id | structured | 0.3 | 1fec8e78-bce4-4aaf-ab1b-5451cc387264 |
| **y** | created_by_user_ids | id | structured | 0.3 | 5dbb24de-25a2-451b-af2a-6be077c485a6 |
| n | graph_item_url | url | structured | 0.3 | https://graph.microsoft.com/v1.0/drives/b!XGpCnRUEXUKIqi8lVd |
| **y** | modified_by_app_ids | id | structured | 0.3 | 1fec8e78-bce4-4aaf-ab1b-5451cc387264 |
| n | conflict_behavior | org | structured | 0.0 | fail |
| n | drive_id | skip | structured | 0.0 | b!XGpCnRUEXUKIqi8lVdI648Fnboi29LJEqDqN_WR8YhJz7ub9PWq2RJLrnh |
| n | drive_type | org | structured | 0.0 | documentLibrary |
| n | file_extension | org | structured | 0.0 | .pdf |
| n | id | skip | structured | 0.0 | 01YRFTPZQE3KXSILK2KBFZJBAH3OYDVOLU |
| n | item_type | org | structured | 0.0 | file |
| n | mime_type | org | structured | 0.0 | application/pdf |
| n | odata_etag | org | structured | 0.0 | "{24AFDA04-5A2D-4B50-9484-07DBB03AB974},2" |
| n | parent_folder_id | skip | structured | 0.0 | 01YRFTPZSAQ3LTC36HPJAKFLAOF5FZ7DE3 |
| n | quick_xor_hash | org | structured | 0.0 | ugGR8IUUjQyVGu9NEG/iMqlflSs= |

## OMLegalStaging  — rows=18, 15 ON

| KEEP? | column | TYPE | mode | conf | sample |
|---|---|---|---|---|---|
| **y** | PRIMARYADDRESSLOCATIONID | phone | structured | 1.0 | 000000012 |
| **y** | PRIMARYCONTACTPHONE | phone | structured | 1.0 | +604-202 1079 |
| **y** | PARTYNUMBER | phone | structured | 0.94 | 000000106 |
| **y** | NAME | person | structured | 0.7 | Blue Fountain Media, Inc. |
| **y** | NAMEALIAS | person | structured | 0.7 | 64-BFMP |
| **y** | ADDRESSZIPCODE | phone | structured | 0.6 | 500081 |
| **y** | LEGALENTITYID | org | structured | 0.6 | BFMP |
| **y** | PRIMARYCONTACTPHONEDESCRIPTION | phone | structured | 0.6 | penang |
| **y** | PRIMARYCONTACTPHONEPURPOSE | phone | structured | 0.6 | Business |
| **y** | ADDRESSDESCRIPTION | org | structured | 0.5 | Blue Fountain Media, Inc. |
| **y** | ADDRESSSTREET | org | structured | 0.5 | 14980 NE 31st Way, Suite 100,  Redmond, WA 98052 |
| **y** | FULLPRIMARYADDRESS | org | structured | 0.5 | 14980 NE 31st Way, Suite 100,  Redmond, WA 98052 USA |
| **y** | ADDRESSCITY | org | structured | 0.0 | Hyderabad |
| **y** | ADDRESSCOUNTRYREGIONID | org | structured | 0.0 | USA |
| **y** | ADDRESSSTATE | org | structured | 0.0 | TS |
| n | ADDRESSLOCATIONROLES | org | structured | 0.5 | Business |
| n | DEFINITIONGROUP | org | structured | 0.5 | LEexporttoDLProd |
| n | ADDRESSCOUNTRYREGIONISOCODE | skip | structured | 0.0 | US |
| n | EXECUTIONID | skip | structured | 0.0 | LEexporttoDLProd-2026-06-13T04:00:04-FEB561DA9A9F41629CEB2DF |
| n | LANGUAGEID | skip | structured | 0.0 | en-US |
| n | PARTITION | org | structured | 0.0 | initial |

## sharepoint_sites  — rows=5, 10 ON

| KEEP? | column | TYPE | mode | conf | sample |
|---|---|---|---|---|---|
| **y** | file_path | freetext | freetext | 1.0 | raff.ripoll@centific.com/sites/owned_sites.json |
| **y** | folder_name | email | structured | 1.0 | raff.ripoll@centific.com |
| **y** | display_name | person | structured | 0.7 | LLM Data |
| **y** | group_name | person | structured | 0.7 | LLM Data |
| **y** | hostname | person | structured | 0.7 | digitaltechedge.sharepoint.com |
| **y** | name | person | structured | 0.7 | LLMData |
| **y** | site_id | freetext | freetext | 0.65 | digitaltechedge.sharepoint.com,9d426a5c-0415-425d-88aa-2f255 |
| **y** | raw_json | freetext | freetext | 0.6 | {"@odata.context": "https://graph.microsoft.com/v1.0/$metada |
| **y** | description | org | structured | 0.5 | Master team hub for LLM Data updates |
| **y** | web_url | url | structured | 0.3 | https://digitaltechedge.sharepoint.com/sites/LLMData |
| n | group_id | id | structured | 0.3 | 64861b16-d6ae-4c64-b505-a84a5cb278af |
| n | odata_context | url | structured | 0.3 | https://graph.microsoft.com/v1.0/$metadata#sites/$entity |
| n | source | org | structured | 0.0 | group |

## teams_chat  — rows=5, 9 ON

| KEEP? | column | TYPE | mode | conf | sample |
|---|---|---|---|---|---|
| **y** | from_user_id | email | structured | 1.0 | michael.underwood@centific.com |
| **y** | folder_name | person | structured | 0.7 | revops-leadership |
| **y** | from_user_display_name | person | structured | 0.7 | Michael Underwood |
| **y** | body_content | freetext | freetext | 0.6 | DM thread for close-plan follow-up with Michael Underwood. |
| **y** | chat_id | freetext | freetext | 0.0 | 19:revops-leadership@thread.v2 |
| **y** | subject | freetext | freetext | 0.0 |  |
| **y** | summary | freetext | freetext | 0.0 |  |
| **y** | web_url | url | structured | 0.0 |  |
| **y** | event_callRecordingUrl | url | structured | 0.0 |  |
| n | _topic | person | structured | 0.5 | #revops-leadership |
| n | _chat_type | org | structured | 0.0 | channel |
| n | body_content_type | org | structured | 0.0 | text |
| n | created_date_time | skip | structured | 0.0 | 2026-06-09T08:00:00Z |
| n | from_user_identity_type | skip | structured | 0.0 | aadUser |
| n | id | skip | structured | 0.0 | fixture-revops-root |
| n | importance | org | structured | 0.0 | normal |
| n | message_type | org | structured | 0.0 | event |

## ts_mstr  — rows=1,035,366, 11 ON

| KEEP? | column | TYPE | mode | conf | sample |
|---|---|---|---|---|---|
| **y** | ApproverEmployeeId | id | structured | 0.7 | System |
| **y** | ApproverEmployeeName | person | structured | 0.7 | System |
| **y** | CategoryName | person | structured | 0.7 | Working time |
| **y** | EmployeeID | id | structured | 0.7 | P0001164 |
| **y** | EmployeeLegalEntity | org | structured | 0.7 | PTTW |
| **y** | EmployeeName | person | structured | 0.7 | Jia Zhang |
| **y** | FileName | person | structured | 0.7 | TSDetails_20240626085821_0628.csv |
| **y** | LastUpdateEmployeeId | id | structured | 0.7 | P3002035 |
| **y** | LastUpdateEmployeeName | person | structured | 0.7 | Rocio Moreno Madirolas |
| **y** | LegalEntity | org | structured | 0.6 | uwhs |
| **y** | Remark | org | structured | 0.0 | 20240730 update status from draft to new |
| n | ProjectGroup | org | structured | 0.5 | FL |
| n | CategoryId | skip | structured | 0.0 | Working time |
| n | LineNumber | skip | structured | 0.0 | 16 |
| n | ProjectId | skip | structured | 0.0 | UWHSAWSSI24P066 |
| n | TimesheetNumber | skip | structured | 0.0 | T2024062608523817863 |
