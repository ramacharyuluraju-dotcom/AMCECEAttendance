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

# Initialize Firebase
# (Safe check to prevent re-initialization error on reload)
if not firebase_admin._apps:
    try:
        # Check for Streamlit Secrets or use local file
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
    """Fetches student list. Cached for speed."""
    c_dept = str(dept).strip().upper()
    c_sem = str(sem).strip()
    c_sec = str(section).strip().upper()
    
    docs = db.collection('Students')\
        .where("dept", "==", c_dept)\
        .where("sem", "==", c_sem)\
        .where("section", "==", c_sec).stream()
    
    return [{"usn": d.id, **d.to_dict()} for d in docs]

@st.cache_data(ttl=10) # Low cache to see reassignments quickly
def get_faculty_courses(faculty_id):
    docs = db.collection('Courses').where("faculty_id", "==", faculty_id).stream()
    return [d.to_dict() for d in docs]

# ==========================================
# 3. DATA HELPERS
# ==========================================

def sanitize_key(val):
    if not val: return ""
    return str(val).strip().upper().replace(".", "_").replace("/", "_").replace(" ", "")

def generate_email(name, existing_email=None):
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
    """VTU Detention Report (Includes students with 0 attendance)"""
    students = get_students_cached(dept, sem, section)
    if not students: return pd.DataFrame()
    
    report_data = []
    
    for s in students:
        usn = s['usn']
        name = s.get('name', 'Unknown')
        ay = s.get('ay', '2025_26')
        
        doc = db.collection('Student_Summaries').document(usn).get()
        structured = {}
        
        if doc.exists:
            raw_data = doc.to_dict()
            for k, v in raw_data.items():
                if "." in k:
                    parts = k.split('.')
                    code, field = parts[0], parts[1]
                    if code not in structured: structured[code] = {}
                    structured[code][field] = v
                elif isinstance(v, dict):
                    structured[k] = v
        
        # If no summary, force fill with course list (0/0)
        if not structured:
            courses = db.collection('Courses').where("dept", "==", dept)\
                .where("sem", "==", sem).where("section", "==", section).stream()
            for c in courses:
                cd = c.to_dict(); sc = sanitize_key(cd['subcode'])
                structured[sc] = {'title': cd['subtitle'], 'total': 0, 'attended': 0}

        for code, stats in structured.items():
            tot = stats.get('total', 0)
            att = stats.get('attended', 0)
            absent = tot - att
            pct = 100.0 if tot == 0 else (att / tot * 100)
            status = "Safe" if pct >= 85 else ("Warning" if pct >= 75 else "Critical")
            
            report_data.append({
                "AY": ay, "Dept": dept, "Sem": sem, "Section": section,
                "USN": usn, "Name": name, 
                "Subject Code": code, "Subject Title": stats.get('title', code),
                "Total Classes": tot, "Classes Attended": att,
                "Classes Absent": absent, "Percentage": round(pct, 2), 
                "Status": status
            })
                
    if report_data:
        return pd.DataFrame(report_data).sort_values(by=['USN', 'Subject Code'])
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

def render_report_tab():
    st.subheader("1. 🎓 VTU Shortage/Detention List")
    c1, c2, c3 = st.columns(3)
    s_dept = c1.selectbox("Department", ["ECE", "CSE", "ISE", "AIML", "MECH", "CIVIL", "EEE"], index=0)
    s_sem = c2.selectbox("Semester", ["1", "2", "3", "4", "5", "6", "7", "8"], index=2)
    s_sec = c3.selectbox("Section", ["A", "B", "C", "D", "E", "F", "G"], index=0)
    
    if st.button("Generate Detention List"):
        with st.spinner("Processing..."):
            df = generate_student_summary_report(s_dept, s_sem, s_sec)
        
        if not df.empty:
            st.dataframe(df)
            st.download_button("⬇️ CSV", df.to_csv(index=False).encode('utf-8'), "VTU_List.csv")
        else:
            st.warning("No data found.")

    st.divider()
    st.subheader("2. 📝 Class Log (Audit)")
    c1, c2 = st.columns(2)
    l_start = c1.date_input("From Date", datetime.date.today().replace(day=1))
    l_end = c2.date_input("To Date", datetime.date.today())
    
    if st.button("Generate Class Logs"):
        df = generate_session_report(s_dept, l_start, l_end) # reusing dept from above
        if not df.empty:
            st.dataframe(df)
            st.download_button("⬇️ Logs CSV", df.to_csv(index=False).encode('utf-8'), "class_logs.csv")
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
            date_val = c_date.date_input("Date", datetime.date.today())
            period_val = c_period.selectbox("Period", ["1", "2", "3", "4", "5", "6", "7", "Lab"])
            
            session_id = f"{date_val}_{course['subcode']}_{course['section']}_{period_val}"
            already_marked = db.collection('Class_Sessions').document(session_id).get().exists
            
            if already_marked:
                st.error("⚠️ Already marked.")
                if not st.checkbox("Unlock to Overwrite?"): st.stop()
            
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
        logs = list(db.collection('Class_Sessions').where("faculty_id", "==", user['id']).order_by("date", "DESCENDING").stream())
        if logs:
            data = []
            for l in logs:
                d = l.to_dict()
                tot = d.get('total_students', 0)
                if tot == 0: tot = len(get_students_cached(d.get('dept','ECE'), d.get('sem','3'), d.get('section','A'))) # Fallback
                present = tot - len(d.get('absentees', []))
                data.append({"Date":d['date'], "Period":d['period'], "Class":d['course_code'], "Present":f"{present}/{tot}"})
            st.dataframe(pd.DataFrame(data), use_container_width=True)
        else:
            st.info("No history.")
    with t3:
        render_report_tab()

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
        render_report_tab()

    with t4:
        st.subheader("Manage Faculty")
        tab_new, tab_manage = st.tabs(["Add New", "Manage & Reassign"])
        
        with tab_new:
            with st.form("add_fac"):
                c1, c2 = st.columns(2)
                n_name = c1.text_input("Name"); n_dept = c2.text_input("Dept")
                n_email = c1.text_input("Email"); n_pass = c2.text_input("Password", type="password")
                if st.form_submit_button("Create"):
                    db.collection('Users').document(n_email).set({"name":n_name, "role":"Faculty", "dept":n_dept, "password":n_pass})
                    st.success("Created")
        
        with tab_manage:
            sel_dept = st.selectbox("Department", ["ECE", "CSE", "ISE", "AIML", "MECH", "CIVIL", "EEE"])
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
                                db.collection('Courses').document(c.id).update({"faculty_id": new_email})
                                st.success("Reassigned")
                                st.rerun()
                else: st.info("No courses.")
            else: st.warning("No faculty found.")

    with t5:
        st.subheader("Manage Students")
        ts, ta = st.tabs(["Search", "Add Manual"])
        with ts:
            s_in = st.text_input("USN").strip().upper()
            if s_in:
                doc = db.collection('Students').document(s_in).get()
                if doc.exists:
                    st.write(doc.to_dict())
                    if st.button("Delete"):
                        db.collection('Students').document(s_in).delete()
                        db.collection('Student_Summaries').document(s_in).delete()
                        st.success("Deleted"); st.rerun()
                else: st.warning("Not found.")
        with ta:
            with st.form("manual_stu"):
                m_usn = st.text_input("USN").upper(); m_name = st.text_input("Name")
                m_dept = st.selectbox("Dept", ["ECE","CSE","ISE"]); m_sem = st.selectbox("Sem",["1","2","3","4","5","6","7","8"])
                m_sec = st.text_input("Sec", "A").upper()
                if st.form_submit_button("Add"):
                    db.collection('Students').document(m_usn).set({"name":m_name,"dept":m_dept,"sem":m_sem,"section":m_sec,"ay":"2025_26"})
                    # Auto Link
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
    usn = c2.text_input("Enter USN").strip().upper()
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
            st.dataframe(df)

def main():
    with st.sidebar:
        st.title("🔐 Login")
        if st.session_state['auth_user']:
            st.success(f"User: {st.session_state['auth_user']['name']}")
            if st.button("Logout"): st.session_state['auth_user'] = None; st.rerun()
        else:
            uid = st.text_input("Email/ID").strip()
            pwd = st.text_input("Password", type="password")
            if st.button("Sign In"):
                if not uid: st.warning("Enter ID"); return
                if uid == "admin" and pwd == "admin123":
                    st.session_state['auth_user'] = {"id":"admin", "name":"Admin", "role":"Admin"}
                    st.rerun()
                else:
                    try:
                        doc = db.collection('Users').document(sanitize_key(uid)).get()
                        if doc.exists and doc.to_dict().get('password') == pwd:
                            st.session_state['auth_user'] = {**doc.to_dict(), "id": sanitize_key(uid)}
                            st.rerun()
                        else: st.error("Invalid Login")
                    except Exception as e: st.error(f"Error: {e}")

    user = st.session_state.get('auth_user')
    if user:
        if user['role'] == "Admin": admin_dashboard()
        elif user['role'] == "Faculty": faculty_dashboard(user)
    else: student_dashboard()

if __name__ == "__main__":
    main()
