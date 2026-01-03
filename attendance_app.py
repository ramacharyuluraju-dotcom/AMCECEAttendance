import streamlit as st
import pandas as pd
import firebase_admin
from firebase_admin import credentials, firestore
import datetime
import altair as alt
import re

# ==========================================
# 1. SETUP & CONFIGURATION
# ==========================================

st.set_page_config(
    page_title="VTU Attendance System", 
    page_icon="🎓", 
    layout="wide"
)

# Session State Initialization
if 'auth_user' not in st.session_state:
    st.session_state['auth_user'] = None
if 'admin_search_usn' not in st.session_state:
    st.session_state['admin_search_usn'] = ""

# Initialize Firebase
if not firebase_admin._apps:
    try:
        # Check if secrets exist (Streamlit Cloud), otherwise look for local file
        if "firebase" in st.secrets:
            key_dict = dict(st.secrets["firebase"])
            cred = credentials.Certificate(key_dict)
        else:
            cred = credentials.Certificate("firebase_key.json")
        firebase_admin.initialize_app(cred)
    except Exception as e:
        st.error(f"Firebase Init Error: {e}")

db = firestore.client()

# ==========================================
# 2. CACHING & OPTIMIZATION
# ==========================================

@st.cache_data(ttl=60) 
def get_students_cached(dept, sem, section):
    c_dept = str(dept).strip().upper()
    c_sem = str(sem).strip()
    c_sec = str(section).strip().upper()
    
    docs = db.collection('Students')\
        .where("dept", "==", c_dept)\
        .where("sem", "==", c_sem)\
        .where("section", "==", c_sec).stream()
    
    return [{"usn": d.id, **d.to_dict()} for d in docs]

@st.cache_data(ttl=10) 
def get_faculty_courses(faculty_id):
    docs = db.collection('Courses').where("faculty_id", "==", faculty_id).stream()
    return [d.to_dict() for d in docs]

# ==========================================
# 3. DATA HELPERS
# ==========================================

def sanitize_key(val):
    """Used for USNs and Course Codes (Uppercase, no dots)"""
    if not val: return ""
    return str(val).strip().upper().replace(".", "_").replace("/", "_").replace(" ", "")

def generate_email(name, existing_email=None):
    """Generates standard emails (Lowercase, dots allowed)"""
    val = str(existing_email).strip().lower()
    if val and val not in ['nan', 'none', '']:
        return val
    clean_name = re.sub(r'[^a-zA-Z0-9]', '.', str(name).strip().lower())
    clean_name = re.sub(r'\.+', '.', clean_name).strip('.')
    return f"{clean_name}@amc.edu"

# ==========================================
# 4. REPORT GENERATORS
# ==========================================

def generate_session_report(dept, start_date, end_date):
    """Class Log Report"""
    all_courses = db.collection('Courses').where("dept", "==", dept).stream()
    course_lookup = {}
    for c in all_courses:
        d = c.to_dict()
        course_lookup[d['subcode']] = {'sem': d.get('sem', 'N/A'), 'title': d.get('subtitle', '')}

    sessions = db.collection('Class_Sessions')\
        .where("date", ">=", str(start_date))\
        .where("date", "<=", str(end_date))\
        .stream()
        
    data = []
    for s in sessions:
        d = s.to_dict()
        subcode = d.get('course_code', '')
        if subcode in course_lookup:
            info = course_lookup[subcode]
            data.append({
                "Date": d.get('date'),
                "Period": d.get('period', 'N/A'),
                "Dept": dept,
                "Sem": info['sem'],
                "Section": d.get('section'),
                "Subject Code": subcode,
                "Subject Title": info['title'],
                "Faculty Name": d.get('faculty_name'),
                "Absentees Count": len(d.get('absentees', [])),
                "Absent USNs": ", ".join(d.get('absentees', []))
            })
    return pd.DataFrame(data)

def generate_student_summary_report(dept, sem, section):
    """Generates a Consolidated Student-wise Report (Pivot Table)"""
    students = get_students_cached(dept, sem, section)
    if not students: return pd.DataFrame()
    
    raw_data = []
    all_subjects = set()
    
    for s in students:
        usn = s['usn']
        name = s.get('name', 'Unknown')
        
        doc = db.collection('Student_Summaries').document(usn).get()
        structured = {}
        
        if doc.exists:
            data = doc.to_dict()
            for k, v in data.items():
                if "." in k:
                    try:
                        code, field = k.split('.')[0], k.split('.')[1]
                        if code not in structured: structured[code] = {}
                        structured[code][field] = v
                    except: pass
        
        student_row = {
            "AY": s.get('ay', '2025_26'),
            "Dept": dept,
            "Sem": sem, 
            "Section": section,
            "USN": usn, 
            "Name": name
        }
        
        # If no summary exists, initialize with 0
        if not structured:
            courses = db.collection('Courses').where("dept", "==", dept)\
                .where("sem", "==", sem).where("section", "==", section).stream()
            for c in courses:
                sc = sanitize_key(c.to_dict()['subcode'])
                student_row[sc] = 0.0
                all_subjects.add(sc)
        else:
            for code, stats in structured.items():
                tot = stats.get('total', 0)
                att = stats.get('attended', 0)
                pct = 100.0 if tot == 0 else round((att / tot * 100), 1)
                student_row[code] = pct
                all_subjects.add(code)
        
        raw_data.append(student_row)

    if raw_data:
        df = pd.DataFrame(raw_data)
        base_cols = ["AY", "Dept", "Sem", "Section", "USN", "Name"]
        subj_cols = sorted(list(all_subjects))
        
        for sc in subj_cols:
            if sc not in df.columns: df[sc] = 0.0
            
        final_cols = base_cols + subj_cols
        return df[final_cols].sort_values(by="USN").fillna(0)
        
    return pd.DataFrame()

# ==========================================
# 5. CSV PROCESSORS
# ==========================================

def process_courses_csv(df):
    df.columns = [str(c).strip().lower().replace(" ", "").replace("_", "") for c in df.columns]
    rename_map = {'email':'facultyemail','mail':'facultyemail','sub':'subcode','code':'subcode','faculty':'facultyname','fac':'facultyname','sec':'section','semester':'sem'}
    df = df.rename(columns=rename_map).fillna("")
    if 'subcode' not in df.columns: return 0, ["❌ Error: Missing SubCode"]

    batch = db.batch(); count = 0; logs = []
    for _, row in df.iterrows():
        raw_code = row.get('subcode', '')
        if not raw_code: continue
        subcode = sanitize_key(raw_code)
        ay = str(row.get('ay', '2025_26')).strip()
        dept = str(row.get('dept', 'ECE')).upper().strip()
        sem = str(row.get('sem', '3')).strip()
        section = str(row.get('section', 'A')).upper().strip()
        fname = str(row.get('facultyname', 'Faculty')).strip()
        femail = generate_email(fname, row.get('facultyemail', ''))
        
        cid = f"{ay}_{dept}_{sem}_{section}_{subcode}"
        batch.set(db.collection('Courses').document(cid), {
            "ay": ay, "dept": dept, "sem": sem, "section": section,
            "subcode": subcode, "subtitle": str(row.get('subtitle', subcode)),
            "faculty_id": femail, "faculty_name": fname
        })
        batch.set(db.collection('Users').document(femail), {
            "name": fname, "role": "Faculty", "dept": dept, "password": "password123"
        }, merge=True)
        logs.append(f"Linked {subcode} -> {femail}")
        count += 1
        if count % 200 == 0: batch.commit(); batch = db.batch()
    batch.commit()
    return count, logs

def process_students_csv(df):
    df.columns = [str(c).strip().lower().replace(" ", "").replace("_", "") for c in df.columns]
    df = df.rename(columns={'sec': 'section', 'semester': 'sem', 'academic': 'ay'}).fillna("")
    if 'usn' not in df.columns: return 0
    
    batch = db.batch(); count = 0
    course_map = {}
    for c in db.collection('Courses').stream():
        d = c.to_dict()
        k = f"{d['dept']}_{d['sem']}_{d['section']}"
        if k not in course_map: course_map[k] = []
        course_map[k].append(d)
        
    for _, row in df.iterrows():
        raw_usn = row.get('usn', '')
        if not raw_usn: continue
        usn = sanitize_key(raw_usn)
        dept = str(row.get('dept', 'ECE')).upper().strip()
        sem = str(row.get('sem', '3')).strip()
        sec = str(row.get('section', 'A')).upper().strip()
        ay = str(row.get('ay', '2025_26')).strip()
        
        batch.set(db.collection('Students').document(usn), {
            "name": row.get('name', 'Student'),
            "dept": dept, "sem": sem, "section": sec, "ay": ay, "batch": str(row.get('batch', ''))
        })
        k = f"{dept}_{sem}_{sec}"
        if k in course_map:
            updates = {}
            for subj in course_map[k]:
                code = sanitize_key(subj['subcode'])
                updates[f"{code}.title"] = subj['subtitle']
                updates[f"{code}.total"] = firestore.Increment(0)
                updates[f"{code}.attended"] = firestore.Increment(0)
            if updates: batch.set(db.collection('Student_Summaries').document(usn), updates, merge=True)
        count += 1
        if count % 200 == 0: batch.commit(); batch = db.batch()
    batch.commit()
    return count

def admin_force_sync():
    students = db.collection('Students').stream()
    courses = list(db.collection('Courses').stream())
    course_map = {}
    for c in courses:
        d = c.to_dict()
        k = f"{str(d['dept']).strip().upper()}_{str(d['sem']).strip()}_{str(d['section']).strip().upper()}"
        if k not in course_map: course_map[k] = []
        course_map[k].append(d)
    
    batch = db.batch(); count = 0; updated = 0
    for s in students:
        s_data = s.to_dict(); usn = s.id
        k = f"{str(s_data.get('dept','')).strip().upper()}_{str(s_data.get('sem','')).strip()}_{str(s_data.get('section','')).strip().upper()}"
        if k in course_map:
            updates = {}
            for c in course_map[k]:
                code = sanitize_key(c['subcode'])
                updates[f"{code}.title"] = c['subtitle']
                updates[f"{code}.total"] = firestore.Increment(0)
                updates[f"{code}.attended"] = firestore.Increment(0)
            batch.set(db.collection('Student_Summaries').document(usn), updates, merge=True)
            updated += 1
        count += 1
        if count % 200 == 0: batch.commit(); batch = db.batch()
    batch.commit()
    return updated

# ==========================================
# 6. DASHBOARDS
# ==========================================

def render_report_tab(prefix=""):
    st.subheader("1. 🎓 Consolidated Detention/Attendance Report")
    c1, c2, c3 = st.columns(3)
    s_dept = c1.selectbox("Department", ["ECE", "CSE", "ISE", "AIML", "MECH", "CIVIL", "EEE"], index=0, key=f'{prefix}rep_dept')
    s_sem = c2.selectbox("Semester", ["1", "2", "3", "4", "5", "6", "7", "8"], index=2, key=f'{prefix}rep_sem')
    s_sec = c3.selectbox("Section", ["A", "B", "C", "D", "E", "F", "G"], index=0, key=f'{prefix}rep_sec')
    
    if st.button("Generate Consolidated Report", key=f'{prefix}btn_cons'):
        with st.spinner("Processing..."):
            df = generate_student_summary_report(s_dept, s_sem, s_sec)
        
        if not df.empty:
            st.success(f"Generated report for {len(df)} students.")
            st.dataframe(df)
            st.download_button("⬇️ Download CSV", df.to_csv(index=False).encode('utf-8'), "Consolidated_Attendance.csv", key=f'{prefix}dl_cons')
        else:
            st.warning("No data found for this class.")

    st.divider()
    st.subheader("2. 📝 Class Log (Audit)")
    c1, c2 = st.columns(2)
    # FIXED: Added unique keys to date_inputs to prevent tab jumping
    l_start = c1.date_input("From Date", datetime.date.today().replace(day=1), key=f'{prefix}rep_start_date')
    l_end = c2.date_input("To Date", datetime.date.today(), key=f'{prefix}rep_end_date')
    
    if st.button("Generate Class Logs", key=f'{prefix}btn_logs'):
        df = generate_session_report(s_dept, l_start, l_end)
        if not df.empty:
            st.dataframe(df)
            st.download_button("⬇️ Logs CSV", df.to_csv(index=False).encode('utf-8'), "class_logs.csv", key=f'{prefix}dl_logs')
        else:
            st.warning("No classes found.")

def faculty_dashboard(user):
    st.title(f"👨‍🏫 {user['name']}")
    my_courses = get_faculty_courses(user['id'])
    
    t1, t2, t3 = st.tabs(["📝 Attendance", "📜 History", "📊 Reports"])
    
    with t1:
        if not my_courses:
            st.warning("No courses assigned.")
        else:
            c_map = {f"{c['subcode']} ({c['section']})" : c for c in my_courses}
            sel_name = st.selectbox("Select Class", list(c_map.keys()))
            course = c_map[sel_name]
            
            st.caption(f"Marking: {course['subtitle']} | {course['dept']} {course['sem']}-{course['section']}")
            
            c_date, c_period = st.columns(2)
            date_val = c_date.date_input("Date", datetime.date.today(), key='mark_date')
            period_val = c_period.selectbox("Period", ["1", "2", "3", "4", "5", "6", "7", "Lab"], key='mark_period')
            
            session_id = f"{date_val}_{course['subcode']}_{course['section']}_{period_val}"
            already_marked = db.collection('Class_Sessions').document(session_id).get().exists
            
            if already_marked:
                st.error("⚠️ Already marked.")
                if not st.checkbox("Unlock to Overwrite?", key='unlock_mark'): st.stop()
            
            s_list = sorted(get_students_cached(course['dept'], course['sem'], course['section']), key=lambda x: x['usn'])
            
            if s_list:
                with st.form("mark"):
                    st.write(f"Total: {len(s_list)}")
                    select_all = st.checkbox("Select All", value=True)
                    cols = st.columns(4); status_map = {}
                    for i, s in enumerate(s_list):
                        status_map[s['usn']] = cols[i%4].checkbox(s['usn'], value=select_all, key=s['usn'])
                    
                    if st.form_submit_button("Submit"):
                        absentees = [u for u, p in status_map.items() if not p]
                        batch = db.batch()
                        batch.set(db.collection('Class_Sessions').document(session_id), {
                            "course_code": course['subcode'], "date": str(date_val),
                            "period": period_val, "faculty_id": user['id'], "faculty_name": user['name'],
                            "total_students": len(s_list), "absentees": absentees, "timestamp": datetime.datetime.now()
                        })
                        
                        if not already_marked:
                            sub_key = sanitize_key(course['subcode'])
                            for s in s_list:
                                ref = db.collection('Student_Summaries').document(s['usn'])
                                batch.set(ref, {f"{sub_key}.title": course['subtitle'], f"{sub_key}.total": firestore.Increment(1)}, merge=True)
                                if s['usn'] not in absentees: batch.set(ref, {f"{sub_key}.attended": firestore.Increment(1)}, merge=True)
                            st.success("✅ Saved!")
                        else:
                            st.warning("Log updated. Stats not incremented.")
                        batch.commit()
    with t2:
        # Fetch logs
        logs_stream = db.collection('Class_Sessions').where("faculty_id", "==", user['id']).stream()
        # Sort in Python to avoid Index Error
        logs = sorted([l for l in logs_stream], key=lambda x: x.to_dict().get('date', ''), reverse=True)
        
        if logs:
            data = []
            for l in logs:
                d = l.to_dict()
                # SAFE GET to avoid crash if field is missing
                d_date = d.get('date', 'Unknown')
                d_period = d.get('period', '-')
                d_code = d.get('course_code', '?')
                tot = d.get('total_students', 0)
                
                # Fallback if total is 0
                if tot == 0: 
                    tot = len(get_students_cached(d.get('dept','ECE'), d.get('sem','3'), d.get('section','A'))) 
                
                present = tot - len(d.get('absentees', []))
                data.append({
                    "Date": d_date, 
                    "Period": d_period, 
                    "Class": d_code, 
                    "Present": f"{present}/{tot}"
                })
            
            st.dataframe(pd.DataFrame(data), use_container_width=True)
        else:
            st.info("No history found.")
    with t3:
        # FIXED: Pass prefix to ensure unique keys
        render_report_tab(prefix="fac_")

def admin_dashboard():
    st.title("⚙️ Admin Dashboard")
    t1, t2, t3, t4, t5 = st.tabs(["📤 Uploads", "🔧 Tools", "📊 Reports", "👨‍🏫 Faculty", "🎓 Students"])
    
    with t1:
        c1, c2 = st.columns(2)
        with c1:
            f1 = st.file_uploader("Courses CSV", type='csv', key='a')
            if f1 and st.button("Process Courses"):
                c, logs = process_courses_csv(pd.read_csv(f1))
                st.success(f"Processed {c} courses.")
        with c2:
            f2 = st.file_uploader("Students CSV", type='csv', key='b')
            if f2 and st.button("Process Students"):
                c = process_students_csv(pd.read_csv(f2))
                st.success(f"Registered {c} students.")

    with t2:
        if st.button("🔄 Sync/Fix All"):
            with st.spinner("Syncing..."): n = admin_force_sync()
            st.success(f"Synced {n} students.")

    with t3:
        # FIXED: Pass prefix to ensure unique keys
        render_report_tab(prefix="adm_")

    with t4:
        st.subheader("Manage Faculty")
        tab_new, tab_manage = st.tabs(["Add New", "Manage & Reassign"])
        
        with tab_new:
            with st.form("add_fac"):
                c1, c2 = st.columns(2)
                n_name = c1.text_input("Name")
                n_dept = c2.text_input("Dept")
                n_email = c1.text_input("Email")
                n_pass = c2.text_input("Password", type="password")
                
                if st.form_submit_button("Create"):
                    clean_email = n_email.strip().lower()
                    if clean_email:
                        db.collection('Users').document(clean_email).set({
                            "name": n_name, 
                            "role": "Faculty", 
                            "dept": n_dept, 
                            "password": n_pass
                        })
                        st.success(f"Created Faculty: {clean_email}")
                    else:
                        st.error("Email is required.")
        
        with tab_manage:
            sel_dept = st.selectbox("Department", ["ECE", "CSE", "ISE", "AIML", "MECH", "CIVIL", "EEE"], key='fac_dept')
            facs = list(db.collection('Users').where("role", "==", "Faculty").where("dept", "==", sel_dept).stream())
            if facs:
                f_map = {f.to_dict()['name']: f.id for f in facs}
                sel_fac = st.selectbox("Select Faculty", list(f_map.keys()))
                fid = f_map[sel_fac]
                
                courses = list(db.collection('Courses').where("faculty_id", "==", fid).stream())
                if courses:
                    for c in courses:
                        cd = c.to_dict()
                        with st.expander(f"{cd['subcode']} - {cd['subtitle']}"):
                            new_email = st.text_input("Reassign to (Email):", key=c.id)
                            if st.button("Update", key=f"btn_{c.id}"):
                                db.collection('Courses').document(c.id).update({"faculty_id": new_email.strip().lower()})
                                st.success("Reassigned")
                                st.rerun()
                else: st.info("No courses.")
            else: st.warning("No faculty found.")

    with t5:
        st.subheader("Manage Students")
        ts, ta = st.tabs(["🔍 Search & Edit", "➕ Add Manual"])
        with ts:
            c_search, c_btn = st.columns([3, 1])
            s_in_raw = c_search.text_input("Enter USN to Search", key="search_usn")
            
            if c_btn.button("🔍 Search", key='search_btn'):
                st.session_state['admin_search_usn'] = s_in_raw.strip().upper()
            
            s_in = st.session_state.get('admin_search_usn', '')
            
            if s_in:
                doc = db.collection('Students').document(s_in).get()
                if doc.exists:
                    d = doc.to_dict()
                    st.markdown("---")
                    # FIXED: Smaller fonts for Admin View
                    st.subheader(f"👤 {d.get('name', 'N/A')}")
                    st.caption(f"USN: {s_in}")
                    
                    with st.container(border=True):
                        c1, c2, c3 = st.columns(3)
                        c1.metric("Dept", d.get('dept', 'N/A'))
                        c2.metric("Sem", d.get('sem', '-'))
                        c3.metric("Section", d.get('section', '-'))

                    st.write("")
                    with st.expander("⚠️ Danger Zone (Delete Student)"):
                        st.warning("Action cannot be undone. Deletes student AND attendance stats.")
                        if st.checkbox(f"I confirm I want to delete {s_in}"):
                            if st.button("🗑️ Permanently Delete"):
                                db.collection('Students').document(s_in).delete()
                                db.collection('Student_Summaries').document(s_in).delete()
                                st.success("Student Deleted Successfully.")
                                st.rerun()
                else:
                    st.warning(f"Student '{s_in}' not found.")
        with ta:
            with st.form("manual_stu"):
                m_usn = st.text_input("USN").upper(); m_name = st.text_input("Name")
                m_dept = st.selectbox("Dept", ["ECE","CSE","ISE"]); m_sem = st.selectbox("Sem",["1","2","3","4","5","6","7","8"])
                m_sec = st.text_input("Sec", "A").upper()
                if st.form_submit_button("Add"):
                    db.collection('Students').document(m_usn).set({"name":m_name,"dept":m_dept,"sem":m_sem,"section":m_sec,"ay":"2025_26"})
                    courses = db.collection('Courses').where("dept", "==", m_dept).where("sem", "==", m_sem).where("section", "==", m_sec).stream()
                    updates = {}
                    for c in courses:
                        k = sanitize_key(c.to_dict()['subcode'])
                        updates[f"{k}.total"] = firestore.Increment(0)
                        updates[f"{k}.attended"] = firestore.Increment(0)
                    if updates: db.collection('Student_Summaries').document(m_usn).set(updates, merge=True)
                    st.success("Added")

def student_dashboard():
    st.markdown("<h1 style='text-align: center;'>🎓 Student Portal</h1>", unsafe_allow_html=True)
    c2 = st.columns([1,2,1])[1]
    usn = c2.text_input("Enter USN", key='std_portal_usn').strip().upper()
    if c2.button("Check Attendance") and usn:
        doc = db.collection('Student_Summaries').document(usn).get()
        if not doc.exists: st.error("USN Not Found"); return
        data = doc.to_dict(); structured = {}
        for k, v in data.items():
            if "." in k:
                p = k.split('.')
                if p[0] not in structured: structured[p[0]] = {}
                structured[p[0]][p[1]] = v
        rows = []
        for c, s in structured.items():
            t = s.get('total',0); a = s.get('attended',0)
            p = 100.0 if t==0 else (a/t*100)
            rows.append({"Subject":c, "Classes":f"{a}/{t}", "Percentage":p})
        if rows:
            df = pd.DataFrame(rows)
            st.metric("Average", f"{df['Percentage'].mean():.1f}%")
            
            # FIXED: Compact chart height and size
            c = alt.Chart(df).mark_bar(size=30).encode(
                x=alt.X('Subject', sort='-y'), 
                y=alt.Y('Percentage', scale=alt.Scale(domain=[0, 100])),
                color=alt.condition(alt.datum.Percentage < 75, alt.value('#FF4B4B'), alt.value('#00CC96')),
                tooltip=['Subject', 'Percentage']
            ).properties(
                height=250 
            )
            st.altair_chart(c, use_container_width=True)
            
            st.dataframe(df)

def main():
    with st.sidebar:
        st.title("🔐 Login")
        if st.session_state['auth_user']:
            st.success(f"User: {st.session_state['auth_user']['name']}")
            st.info(f"ID: {st.session_state['auth_user']['id']}")
            if st.button("Logout"): 
                st.session_state['auth_user'] = None
                st.rerun()
        else:
            uid = st.text_input("Email/ID").strip()
            pwd = st.text_input("Password", type="password").strip()
            
            if st.button("Sign In"):
                if not uid: 
                    st.warning("Please enter your ID/Email")
                    return
                
                if uid == "admin" and pwd == "admin123":
                    st.session_state['auth_user'] = {"id":"admin", "name":"Admin", "role":"Admin"}
                    st.rerun()
                
                # Smart Search
                try:
                    v1 = uid.lower()
                    v2 = sanitize_key(uid)
                    v3 = uid

                    target_doc = None
                    final_id = None

                    doc = db.collection('Users').document(v1).get()
                    if doc.exists: target_doc = doc; final_id = v1
                    
                    if not target_doc:
                        doc = db.collection('Users').document(v2).get()
                        if doc.exists: target_doc = doc; final_id = v2

                    if not target_doc:
                        doc = db.collection('Users').document(v3).get()
                        if doc.exists: target_doc = doc; final_id = v3

                    if target_doc:
                        user_data = target_doc.to_dict()
                        if user_data.get('password') == pwd:
                            st.session_state['auth_user'] = {**user_data, "id": final_id}
                            st.success("Login Successful!")
                            st.rerun()
                        else:
                            st.error("❌ Incorrect Password")
                    else:
                        st.error(f"❌ User ID not found.")
                        
                except Exception as e: 
                    st.error(f"System Error: {e}")

    user = st.session_state.get('auth_user')
    if user:
        if user['role'] == "Admin": admin_dashboard()
        elif user['role'] == "Faculty": faculty_dashboard(user)
    else: 
        student_dashboard()

if __name__ == "__main__":
    main()
