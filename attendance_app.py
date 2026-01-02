import streamlit as st
import pandas as pd
import firebase_admin
from firebase_admin import credentials, firestore
import datetime
import altair as alt
import re

# ==========================================
# 1. CONFIGURATION & CACHING
# ==========================================

st.set_page_config(page_title="VTU Attendance System", page_icon="🎓", layout="wide")

if 'auth_user' not in st.session_state:
    st.session_state['auth_user'] = None

# Initialize Firebase (Robust)
if not firebase_admin._apps:
    try:
        if "firebase" in st.secrets:
            key_dict = dict(st.secrets["firebase"])
            cred = credentials.Certificate(key_dict)
        else:
            cred = credentials.Certificate("firebase_key.json")
        firebase_admin.initialize_app(cred)
    except Exception as e:
        pass # Ignore if already initialized

db = firestore.client()

# --- OPTIMIZATION: CACHED READS ---
@st.cache_data(ttl=3600) # Cache for 1 hour
def get_students_cached(dept, sem, section):
    """Fetches student list once and keeps it in memory."""
    docs = db.collection('Students')\
        .where("dept", "==", dept)\
        .where("sem", "==", sem)\
        .where("section", "==", section).stream()
    
    return [{"usn": d.id, "name": d.to_dict().get('name', 'Student')} for d in docs]

@st.cache_data(ttl=600) # Cache course list for 10 mins
def get_faculty_courses(faculty_id):
    docs = db.collection('Courses').where("faculty_id", "==", faculty_id).stream()
    return [d.to_dict() for d in docs]

# ==========================================
# 2. DATA PROCESSING (CSV)
# ==========================================

def clean_email(val, name_fallback):
    val = str(val).strip().lower()
    if not val or val in ['nan', 'none', '']:
        sanitized = re.sub(r'[^a-zA-Z0-9]', '.', str(name_fallback).strip().lower())
        return f"{sanitized}@amc.edu"
    return val

def batch_process_part_a(df):
    """Upload Courses"""
    df.columns = [str(c).strip().lower().replace(" ", "").replace("_", "") for c in df.columns]
    rename_map = {
        'email': 'facultyemail', 'mail': 'facultyemail',
        'sub': 'subcode', 'code': 'subcode',
        'faculty': 'facultyname', 'fac': 'facultyname',
        'sec': 'section', 'semester': 'sem'
    }
    df = df.rename(columns=rename_map).fillna("")
    
    if 'subcode' not in df.columns: return 0, ["Error: Missing SubCode"]

    batch = db.batch()
    count = 0
    logs = []
    
    for _, row in df.iterrows():
        if not row['subcode']: continue
        
        # Data Prep
        ay = str(row.get('ay', '2025_26')).strip()
        dept = str(row.get('dept', 'ECE')).upper().strip()
        sem = str(row.get('sem', '3')).strip()
        section = str(row.get('section', 'A')).upper().strip()
        subcode = str(row['subcode']).strip().upper()
        fname = str(row.get('facultyname', 'Faculty')).strip()
        femail = clean_email(row.get('facultyemail', ''), fname)
        
        cid = f"{ay}_{dept}_{sem}_{section}_{subcode}"
        
        # Course Doc
        batch.set(db.collection('Courses').document(cid), {
            "ay": ay, "dept": dept, "sem": sem, "section": section,
            "subcode": subcode, "subtitle": str(row.get('subtitle', subcode)),
            "faculty_id": femail, "faculty_name": fname
        })
        
        # Faculty Login
        batch.set(db.collection('Users').document(femail), {
            "name": fname, "role": "Faculty", "dept": dept, "password": "password123"
        }, merge=True)
        
        logs.append(f"Linked {subcode} -> {femail}")
        count += 1
        if count % 200 == 0:
            batch.commit()
            batch = db.batch()
            
    batch.commit()
    return count, logs

def batch_process_part_b(df):
    """Upload Students"""
    df.columns = [str(c).strip().lower().replace(" ", "").replace("_", "") for c in df.columns]
    df = df.rename(columns={'sec': 'section', 'semester': 'sem', 'academic': 'ay'}).fillna("")
    
    if 'usn' not in df.columns: return 0
    
    batch = db.batch()
    count = 0
    
    # Cache Course Map for linking
    course_map = {}
    for c in db.collection('Courses').stream():
        d = c.to_dict()
        key = f"{d['dept']}_{d['sem']}_{d['section']}"
        if key not in course_map: course_map[key] = []
        course_map[key].append(d)
        
    for _, row in df.iterrows():
        if not row['usn']: continue
        
        usn = str(row['usn']).upper().strip()
        dept = str(row.get('dept', 'ECE')).upper().strip()
        sem = str(row.get('sem', '3')).strip()
        sec = str(row.get('section', 'A')).upper().strip()
        
        batch.set(db.collection('Students').document(usn), {
            "name": row.get('name', 'Student'),
            "dept": dept, "sem": sem, "section": sec,
            "ay": str(row.get('ay', ''))
        })
        
        # Link Subjects
        key = f"{dept}_{sem}_{sec}"
        if key in course_map:
            summ_ref = db.collection('Student_Summaries').document(usn)
            updates = {}
            for subj in course_map[key]:
                code = subj['subcode']
                # Initializing to 0 (Safe merge)
                updates[f"{code}.total"] = 0
                updates[f"{code}.attended"] = 0
                updates[f"{code}.title"] = subj['subtitle']
            batch.set(summ_ref, updates, merge=True)
            
        count += 1
        if count % 200 == 0:
            batch.commit()
            batch = db.batch()
            
    batch.commit()
    return count

# ==========================================
# 3. FACULTY DASHBOARD (OPTIMIZED)
# ==========================================

def faculty_dashboard(user):
    st.header(f"👨‍🏫 Welcome, {user['name']}")
    
    # 1. Get Courses (Cached)
    courses = get_faculty_courses(user['id'])
    
    if not courses:
        st.warning("No courses assigned to you.")
        return

    # Dropdown
    c_map = {f"{c['subcode']} ({c['section']})" : c for c in courses}
    sel = st.selectbox("Select Class", list(c_map.keys()))
    course = c_map[sel]
    
    # Tabs for Action vs History
    t1, t2 = st.tabs(["📝 Mark Attendance", "🕒 My History"])
    
    with t1:
        st.info(f"Marking: **{course['subtitle']}** ({course['subcode']})")
        
        # Refresh Button for Cache
        if st.button("🔄 Refresh Student List"):
            get_students_cached.clear()
            st.rerun()
            
        s_list = get_students_cached(course['dept'], course['sem'], course['section'])
        s_list = sorted(s_list, key=lambda x: x['usn'])
        
        if not s_list:
            st.error("No students found in this section.")
            return

        # Form
        with st.form("att_form"):
            c1, c2 = st.columns([1, 2])
            dt = c1.date_input("Date", datetime.date.today())
            fname = c2.text_input("Faculty Name", value=course['faculty_name'])
            
            st.write(f"**Class Strength: {len(s_list)}**")
            
            select_all = st.checkbox("Select All Present", value=True)
            
            status = {}
            cols = st.columns(4)
            for i, s in enumerate(s_list):
                status[s['usn']] = cols[i%4].checkbox(s['usn'], value=select_all)
                
            if st.form_submit_button("💾 Submit Attendance"):
                absentees = [k for k,v in status.items() if not v]
                
                batch = db.batch()
                
                # A. Log Session
                sess_ref = db.collection('Class_Sessions').document() 
                batch.set(sess_ref, {
                    "course_code": course['subcode'],
                    "section": course['section'],
                    "date": str(dt),
                    "faculty_id": user['id'],
                    "faculty_name": fname,
                    "absentees": absentees,
                    "timestamp": datetime.datetime.now()
                })
                
                # B. Update Student Stats (CRITICAL FIX)
                for s in s_list:
                    ref = db.collection('Student_Summaries').document(s['usn'])
                    key = course['subcode']
                    
                    batch.set(ref, {
                        f"{key}.title": course['subtitle'],
                        f"{key}.total": firestore.Increment(1)
                    }, merge=True)
                    
                    if s['usn'] not in absentees:
                        batch.set(ref, {
                            f"{key}.attended": firestore.Increment(1)
                        }, merge=True)
                
                batch.commit()
                st.success("Attendance Saved! Check 'My History'.")
                
    with t2:
        # --- CRITICAL FIX FOR ERROR ---
        # Instead of sorting in DB (which requires index), we sort in Python
        logs_stream = db.collection('Class_Sessions')\
            .where("faculty_id", "==", user['id'])\
            .stream()
            
        # Convert to list
        all_logs = [l.to_dict() for l in logs_stream]
        
        # Sort in Python (Reverse Time)
        all_logs.sort(key=lambda x: x['timestamp'], reverse=True)
        
        # Take Top 10
        recent_logs = all_logs[:10]
        
        data = []
        for d in recent_logs:
            data.append({
                "Date": d['date'],
                "Subject": d['course_code'],
                "Section": d['section'],
                "Absentees": len(d['absentees'])
            })
        
        if data:
            st.dataframe(pd.DataFrame(data), use_container_width=True)
        else:
            st.info("No recent history found.")

# ==========================================
# 4. STUDENT DASHBOARD
# ==========================================

def student_public_dashboard():
    st.markdown("<h1 style='text-align: center;'>🎓 Student Portal</h1>", unsafe_allow_html=True)
    
    c1, c2, c3 = st.columns([1, 2, 1])
    with c2:
        usn = st.text_input("Enter USN", placeholder="1MV20CS001").strip().upper()
        if st.button("Check Attendance", use_container_width=True):
            if not usn: return
            
            doc = db.collection('Student_Summaries').document(usn).get()
            if not doc.exists:
                st.error("USN not found.")
                return
                
            data = doc.to_dict()
            rows = []
            for sub, stats in data.items():
                if isinstance(stats, dict) and 'total' in stats:
                    tot = stats['total']
                    att = stats.get('attended', 0)
                    pct = (att/tot*100) if tot > 0 else 0
                    status = "Safe" if pct >= 85 else ("Warning" if pct >= 75 else "Critical")
                    rows.append({
                        "Subject": sub, "Percentage": pct, 
                        "Status": status, "Classes": f"{att}/{tot}"
                    })
            
            if rows:
                df = pd.DataFrame(rows)
                st.divider()
                
                m1, m2, m3 = st.columns(3)
                m1.metric("Overall Avg", f"{df['Percentage'].mean():.1f}%")
                m2.metric("Critical Subjects", len(df[df['Status']=='Critical']))
                
                c = alt.Chart(df).mark_bar().encode(
                    x='Subject', 
                    y=alt.Y('Percentage', scale=alt.Scale(domain=[0,100])),
                    color=alt.Color('Percentage', scale=alt.Scale(domain=[0, 75, 85, 100], range=['red', 'orange', 'green', 'green']), legend=None)
                ).properties(height=300)
                st.altair_chart(c, use_container_width=True)
                
                st.dataframe(df, use_container_width=True)
            else:
                st.info("No attendance data found yet.")

# ==========================================
# 5. ADMIN DASHBOARD
# ==========================================

def admin_dashboard():
    st.title("⚙️ Admin Dashboard")
    tabs = st.tabs(["📤 Uploads", "👨‍🏫 Faculty", "🎓 Students"])
    
    with tabs[0]:
        c1, c2 = st.columns(2)
        with c1:
            st.subheader("Courses (Part A)")
            f1 = st.file_uploader("Upload Courses CSV", type='csv')
            if f1 and st.button("Process Courses"):
                c, logs = batch_process_part_a(pd.read_csv(f1))
                st.success(f"Processed {c} courses.")
                if logs: st.expander("Logs").write(logs)
        with c2:
            st.subheader("Students (Part B)")
            f2 = st.file_uploader("Upload Students CSV", type='csv')
            if f2 and st.button("Process Students"):
                c = batch_process_part_b(pd.read_csv(f2))
                st.success(f"Registered {c} students.")

    with tabs[1]:
        if st.button("Load Faculty List"):
            docs = db.collection('Users').where("role", "==", "Faculty").stream()
            st.dataframe(pd.DataFrame([d.to_dict() for d in docs]))

    with tabs[2]:
        c1, c2, c3 = st.columns(3)
        dept = c1.text_input("Dept", "ECE")
        sem = c2.text_input("Sem", "3")
        sec = c3.text_input("Sec", "A")
        if st.button("Search Students"):
            st.dataframe(pd.DataFrame(get_students_cached(dept, sem, sec)))
            get_students_cached.clear()

# ==========================================
# 6. MAIN ROUTER
# ==========================================

def main():
    with st.sidebar:
        st.title("Staff Login")
        if st.session_state['auth_user']:
            st.success(f"Hi, {st.session_state['auth_user']['name']}")
            if st.button("Logout"):
                st.session_state['auth_user'] = None
                st.rerun()
        else:
            uid = st.text_input("Email/ID")
            pwd = st.text_input("Password", type="password")
            if st.button("Login"):
                if uid == "admin" and pwd == "admin123":
                    st.session_state['auth_user'] = {"id": "admin", "name": "Admin", "role": "Admin"}
                    st.rerun()
                else:
                    doc = db.collection('Users').document(uid).get()
                    if doc.exists and doc.to_dict().get('password') == pwd:
                        u = doc.to_dict()
                        u['id'] = uid
                        st.session_state['auth_user'] = u
                        st.rerun()
                    else:
                        st.error("Invalid Credentials")

    user = st.session_state['auth_user']
    if user:
        if user['role'] == "Admin": admin_dashboard()
        elif user['role'] == "Faculty": faculty_dashboard(user)
    else:
        student_public_dashboard()

if __name__ == "__main__":
    main()
