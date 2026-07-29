#!/usr/bin/env python3
"""
Free-text / embedded-PII scrubber for the kiran working copy.
Dictionary (owner-approved, safe): multi-token Names (>=6 chars) + CompanyName (>=5) + Email.
Matching: case-insensitive, word-boundary, longest-match. Replace embedded originals with map fakes.
Read/write large HTML text via COPY CSV; write back by ctid (no PK needed).
Usage: phase_freetext_scrub.py [dry|apply]
"""
import pyodbc, ahocorasick, subprocess, csv, io, sys
csv.field_size_limit(100_000_000)

CS=("DRIVER={ODBC Driver 17 for SQL Server};SERVER=obi-poc-server.database.windows.net;DATABASE=obi-sql-db;UID=obi_admin;PWD=__SET_VIA_ENV__;Encrypt=yes;TrustServerCertificate=no;")
COLS=[('project_stage','VNSCUSTCLASSIFICATIONID'),('project_stage','C3'),('project_stage','VNSMASTERDIVISION'),
      ('pwsheader','Opportunity_masterdivision'),('pwsheader','Opportunity_businesssegment'),('pwsheader','ProductLine'),
      ('dyncrm_account','new_accountnamebybu')]

def load_automaton():
    cn=pyodbc.connect(CS,timeout=120,autocommit=True);cur=cn.cursor()
    cur.execute("""SELECT originalvalue, anonymizedvalue FROM obi.mapping_slice
      WHERE anonymizedvalue IS NOT NULL AND originalvalue IS NOT NULL AND (
        (description='Names' AND CHARINDEX(' ',originalvalue)>0 AND LEN(originalvalue)>=6)
        OR (description='CompanyName' AND LEN(originalvalue)>=5)
        OR (description='Email'))""")
    A=ahocorasick.Automaton(); n=0
    for orig,fake in cur.fetchall():
        if not orig or not fake or fake.strip().lower()==orig.strip().lower(): continue
        k=orig.lower()
        if k in A:  # keep longest original for a given lowered key
            continue
        A.add_word(k,(len(k),fake)); n+=1
    cn.close(); A.make_automaton()
    print(f"automaton: {n:,} patterns")
    return A

def scrub(text,A):
    low=text.lower(); matches=[]
    for end,(klen,fake) in A.iter(low):
        start=end-klen+1
        before=text[start-1] if start>0 else ''
        after=text[end+1] if end+1<len(text) else ''
        if (before=='' or not before.isalnum()) and (after=='' or not after.isalnum()):
            matches.append((start,end,klen,fake))
    if not matches: return text,0
    matches.sort(key=lambda m:(m[0],-m[2]))
    chosen=[]; lastend=-1
    for s,e,kl,fk in matches:
        if s>lastend: chosen.append((s,e,fk)); lastend=e
    out=[];i=0
    for s,e,fk in chosen: out.append(text[i:s]); out.append(fk); i=e+1
    out.append(text[i:])
    return ''.join(out), len(chosen)

def read_col(t,c):
    sql='\\copy (SELECT ctid::text, "%s" FROM obi."%s" WHERE "%s" IS NOT NULL AND length("%s")>0) TO STDOUT WITH (FORMAT csv)'%(c,t,c,c)
    r=subprocess.run(['docker','exec','-i','obi_kiran_pg','psql','-U','postgres','-d','kiran','-c',sql],capture_output=True,text=True,encoding='utf-8',errors='surrogatepass')
    if r.returncode: raise RuntimeError(r.stderr[:200])
    return list(csv.reader(io.StringIO(r.stdout)))

def write_updates(t,c,updates):   # updates: list of (ctid, newtext)
    buf=io.StringIO(); w=csv.writer(buf,lineterminator='\n',quoting=csv.QUOTE_ALL)
    for ctid,val in updates: w.writerow([ctid,val])
    # single piped psql script: temp table, \copy inline data, UPDATE by ctid
    script=("DROP TABLE IF EXISTS _ft;\nCREATE TEMP TABLE _ft(rid text, val text);\n"
            "\\copy _ft FROM STDIN WITH (FORMAT csv)\n"+buf.getvalue()+"\\.\n"
            'UPDATE obi."%s" t SET "%s"=f.val FROM _ft f WHERE t.ctid=f.rid::tid;\n'%(t,c))
    r=subprocess.run(['docker','exec','-i','obi_kiran_pg','psql','-U','postgres','-d','kiran','-v','ON_ERROR_STOP=1'],
                     input=script,capture_output=True,text=True,encoding='utf-8',errors='surrogatepass')
    if r.returncode: raise RuntimeError((r.stderr or '')[:300])

def col_maxlen(t,c):
    sql="SELECT coalesce(character_maximum_length,2147483647) FROM information_schema.columns WHERE table_schema='obi' AND table_name='%s' AND column_name='%s';"%(t,c)
    r=subprocess.run(['docker','exec','-i','obi_kiran_pg','psql','-U','postgres','-d','kiran','-tAc',sql],capture_output=True,text=True,encoding='utf-8',errors='surrogatepass')
    try: return int(r.stdout.strip())
    except: return 2147483647

def main(mode):
    A=load_automaton()
    grand_cells=0; grand_repl=0; grand_skip=0
    for t,c in COLS:
        maxlen=col_maxlen(t,c)
        rows=read_col(t,c)
        cells=0; repl=0; skip=0; updates=[]
        for row in rows:
            if len(row)<2: continue
            ctid,text=row[0],row[1]
            new,k=scrub(text,A)
            if k>0:
                if len(new)>maxlen:            # would overflow column -> skip (leave unchanged)
                    skip+=1; continue
                cells+=1; repl+=k
                if mode=='apply': updates.append((ctid,new))
        if mode=='apply' and updates: write_updates(t,c,updates)
        print(f"   {t}.{c}: cells_changed={cells}  replacements={repl}  skipped_overflow={skip} (maxlen={maxlen})")
        grand_cells+=cells; grand_repl+=repl; grand_skip+=skip
    print(f"\nTOTAL: cells_changed={grand_cells}  replacements={grand_repl}  skipped_overflow={grand_skip}  ({'APPLIED' if mode=='apply' else 'DRY-RUN'})")

if __name__=='__main__':
    main(sys.argv[1] if len(sys.argv)>1 else 'dry')
