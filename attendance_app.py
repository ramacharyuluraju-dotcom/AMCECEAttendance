import streamlit as st
import pandas as pd
import firebase_admin
from firebase_admin import credentials, firestore
import datetime
import altair as alt

# ==========================================
# 1. ROBUST SETUP & SESSION STATE
# ==========================================

st.set_page_config(page_title="VTU Attendance System", page_icon="🎓", layout="wide")

# Initialize Session State ONLY ONCE to prevent logouts
if 'auth_user' not in st.session_state:
    st.session_state['auth_user'] = None

# Initialize Firebase
if not firebase_admin._apps:
    try:
        # 1. Cloud Secrets Strategy
        key_dict = dict(st.secrets["firebase"])
        cred = credentials.Certificate(key_dict)
    except:
        # 2. Local File Strategy
        try:
            cred = credentials.Certificate("firebase_key.json")
        except:
            st.error("🚨 Critical Error: No 'firebase_key.json' found. Please check deployment instructions.")
            st.stop()
            
    BUCKET_NAME = "your-project-id.appspot.com" 
    firebase_admin.initialize_app(cred)

db = firestore.client()

# ==========================================
# 2. SMART DATA PROCESSING
# ==========================================

def batch_process_part_a(df):
    """
    Part A: Courses & Faculty
    Smart mapping for columns like 'Email', 'Mail', 'Sub Code'
    """
    # 1. Normalize Headers
    df.columns = [str(c).strip().lower().replace(" ", "").replace("_", "") for c in df.columns]
    
    # 2. Smart Rename Map
    rename_map = {
        'email': 'facultyemail', 'facemail': 'facultyemail', 'mail': 'facultyemail',
        'sub': 'subcode', 'subjectcode': 'subcode', 'code': 'subcode',
        'faculty': 'facultyname', 'facname': 'facultyname', 'fac': 'facultyname',
        'sec': 'section', 'semister': 'sem', 'semester': 'sem'
    }
    df = df.rename(columns=rename_map)
    
    # 3. Validation
    required = ['subcode', 'facultyemail']
    missing = [c for c in required if c not in df.columns]
    
    if missing:
        return 0, [f"❌ Error: CSV missing columns {missing}. Found: {list(df.columns)}"]

    df = df.fillna("")
    batch = db.batch()
    count = 0
    logs = []
    
    for idx, row in df.iterrows():
        # Skip invalid rows
        if not row['subcode'] or not row['facultyemail']:
            continue
            
        # Defaults if columns missing
        ay = str(row.get('ay', '2024')).strip()
        dept = str(row.get('dept', 'CS')).upper().strip()
        sem = str(row.get('sem', '1')).strip()
        section = str(row.get('section', 'A')).upper().strip()
        
        cid = f"{ay}_{dept}_{sem}_{section}_{row['subcode'].upper()}"
        
        # 1. Set Course
        batch.set(db.collection('Courses').document(cid), {
            "ay": ay, "dept": dept, "sem": sem, "section": section,
            "subcode": str(row['subcode']).upper(),
            "subtitle": str(row.get('subtitle', row['subcode'])),
            "faculty_id": str(row['facultyemail']).lower().strip(),
            "faculty_name": str(row.get('facultyname', 'Faculty'))
        })
        
        # 2. Set Faculty Login (Merge to avoid overwriting existing pwd)
        fid = str(row['facultyemail']).lower().strip()
        user_ref = db.collection('Users').document(fid)
        batch.set(user_ref, {
            "name": str(row.get('facultyname', 'Faculty')),
            "role": "Faculty",
            "dept": dept,
            "password": "password123" # Default, change manually if needed
        }, merge=True)
        
        logs.append(f"Linked {row['subcode']} -> {fid}")
        count += 1
        
        if count % 200 == 0:
            batch.commit()
            batch = db.batch()
            
    batch.commit()
    return count, logs

def batch_process_part_b(df):
    """Part B: Students"""
    df.columns = [str(c).strip().lower().replace(" ", "").replace("_", "") for c in df.columns]
    
    # Smart Rename
    rename_map = {'sec': 'section', 'semester': 'sem'}
    df = df.rename(columns=rename_map)
    
    if 'usn' not in df.columns:
        return 0
        
    df = df.fillna("")
    batch = db.batch()
    count = 0
    
    # Pre-fetch courses for Auto-Linking
    course_map = {}
    for c in db.collection('Courses').stream():
        d = c.to_dict()
        key = f"{d['dept']}_{d['sem']}_{d['section']}"
        if key not in course_map: course_map[key] = []
        course_map[key].append(d)
        
    for _, row in df.iterrows():
        if not row['usn']: continue
        
        usn = str(row['usn']).upper().strip()
        dept = str(row.get('dept', 'CS')).upper().strip()
        sem = str(row.get('sem', '1')).strip()
        sec = str(row.get('section', 'A')).upper().strip()
        
        # 1. Create Student
        batch.set(db.collection('Students').document(usn), {
            "name": row.get('name', 'Student'),
            "dept": dept, "sem": sem, "section": sec,
            "batch": str(row.get('batch', ''))
        })
        
        # 2. Auto-Link
        key = f"{dept}_{sem}_{sec}"
        if key in course_map:
            summ_ref = db.collection('Student_Summaries').document(usn)
            updates = {}
            for subj in course_map[key]:
                code = subj['subcode']
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
# 3. ADMIN DASHBOARD (Fixes: Dropdowns & Visibility)
# ==========================================

def tab_bulk_upload():
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("📤 1. Courses (Part A)")
        f1 = st.file_uploader("Upload CSV (Part A)", type='csv', key='a')
        if f1 and st.button("Process Part A"):
            c, logs = batch_process_part_a(pd.read_csv(f1))
            st.success(f"Processed {c} records.")
            if logs:
                with st.expander("Show Logs"):
                    st.write(logs)
    with c2:
        st.subheader("📤 2. Students (Part B)")
        f2 = st.file_uploader("Upload CSV (Part B)", type='csv', key='b')
        if f2 and st.button("Process Part B"):
            c = batch_process_part_b(pd.read_csv(f2))
            st.success(f"Registered {c} students.")

def tab_manage_faculty():
    st.subheader("👨‍🏫 Faculty Management")
    
    with st.expander("➕ Add Single Faculty"):
        c1, c2 = st.columns(2)
        em = c1.text_input("Email").strip().lower()
        nm = c2.text_input("Name")
        if st.button("Create Faculty Login"):
            db.collection('Users').document(em).set({
                "name": nm, "password": "password123", "role": "Faculty"
            })
            st.success("Created!")

    st.write("### 📋 Faculty List")
    if st.button("Load All Faculty"):
        docs = db.collection('Users').where("role", "==", "Faculty").stream()
        data = [{"Email": d.id, **d.to_dict()} for d in docs]
        if data:
            st.dataframe(pd.DataFrame(data), use_container_width=True)
        else:
            st.info("No faculty found.")

def tab_manage_students():
    st.subheader("🎓 Student Management")
    
    # 1. FILTER TOOL (Fixes "No Dropdown" issue)
    st.info("Step 1: Find Students")
    c1, c2, c3 = st.columns(3)
    f_dept = c1.text_input("Dept", "CS")
    f_sem = c2.text_input("Sem", "5")
    f_sec = c3.text_input("Section", "A")
    
    # State to hold search results to prevent disappear on rerun
    if 'search_results' not in st.session_state: st.session_state['search_results'] = []

    if st.button("🔍 Search Class"):
        docs = db.collection('Students')\
            .where("dept", "==", f_dept)\
            .where("sem", "==", f_sem)\
            .where("section", "==", f_sec).stream()
        st.session_state['search_results'] = [{"USN": d.id, **d.to_dict()} for d in docs]
    
    # Display Results with USN
    if st.session_state['search_results']:
        df_res = pd.DataFrame(st.session_state['search_results'])
        st.dataframe(df_res, use_container_width=True)
        
        # 2. EDIT TOOL
        st.divider()
        st.write("### ✏️ Edit Student")
        # Populate dropdown with USNs found
        usn_list = df_res['USN'].tolist()
        sel_usn = st.selectbox("Select USN to Edit", usn_list)
        
        if sel_usn:
            # Fetch current data
            curr = next((item for item in st.session_state['search_results'] if item["USN"] == sel_usn), {})
            
            with st.form("edit_stu"):
                en = st.text_input("Name", value=curr.get('name', ''))
                ed = st.text_input("Dept", value=curr.get('dept', ''))
                es = st.text_input("Sem", value=curr.get('sem', ''))
                esec = st.text_input("Section", value=curr.get('section', ''))
                
                if st.form_submit_button("Update Student"):
                    db.collection('Students').document(sel_usn).update({
                        "name": en, "dept": ed, "sem": es, "section": esec
                    })
                    st.success(f"Updated {sel_usn}!")
                    # Clear cache to force refresh next time
                    st.session_state['search_results'] = []
    else:
        st.write("No students loaded yet.")

def admin_dashboard():
    st.title("⚙️ Super Admin Dashboard")
    tabs = st.tabs(["📤 Uploads", "👨‍🏫 Faculty", "🎓 Students"])
    with tabs[0]: tab_bulk_upload()
    with tabs[1]: tab_manage_faculty()
    with tabs[2]: tab_manage_students()

# ==========================================
# 4. FACULTY DASHBOARD
# ==========================================

def faculty_dashboard(user):
    st.header(f"👨‍🏫 Welcome, {user['name']}")
    
    # 1. Get Courses
    courses = list(db.collection('Courses').where("faculty_id", "==", user['id']).stream())
    
    if not courses:
        st.warning("No courses assigned.")
        return

    # Dropdown
    c_map = {f"{c.to_dict()['subcode']} ({c.to_dict()['section']})" : c.to_dict() for c in courses}
    sel = st.selectbox("Select Class", list(c_map.keys()))
    course = c_map[sel]
    
    st.divider()
    
    # 2. Get Students
    students = db.collection('Students')\
        .where("dept", "==", course['dept'])\
        .where("sem", "==", course['sem'])\
        .where("section", "==", course['section']).stream()
        
    s_list = sorted([{"usn": d.id, "name": d.to_dict()['name']} for d in students], key=lambda x: x['usn'])
    
    if not s_list:
        st.error("No students found.")
        return

    # 3. Mark Attendance
    with st.form("att_form"):
        c1, c2 = st.columns([1, 2])
        dt = c1.date_input("Date", datetime.date.today())
        # Editable Name
        fname = c2.text_input("Faculty Handling Class", value=course['faculty_name'])
        
        st.write("### Student List")
        status = {}
        cols = st.columns(4)
        for i, s in enumerate(s_list):
            status[s['usn']] = cols[i%4].checkbox(f"{s['usn']}", value=True)
            
        if st.form_submit_button("💾 Save Attendance"):
            absentees = [k for k,v in status.items() if not v]
            batch = db.batch()
            
            # Log
            batch.set(db.collection('Class_Sessions').document(), {
                "course_code": course['subcode'],
                "section": course['section'],
                "date": str(dt),
                "faculty_name": fname,
                "absentees": absentees,
                "timestamp": datetime.datetime.now()
            })
            
            # Update
            for s in s_list:
                ref = db.collection('Student_Summaries').document(s['usn'])
                key = course['subcode']
                batch.set(ref, {
                    f"{key}.total": firestore.Increment(1),
                    f"{key}.title": course['subtitle']
                }, merge=True)
                
                if s['usn'] not in absentees:
                    batch.set(ref, {f"{key}.attended": firestore.Increment(1)}, merge=True)
            
            batch.commit()
            st.success("Attendance Saved!")

# ==========================================
# 5. STUDENT DASHBOARD
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
                    rows.append({"Subject": sub, "Percentage": pct, "Status": status, "Classes": f"{att}/{tot}"})
            
            if rows:
                df = pd.DataFrame(rows)
                st.divider()
                # Chart
                c = alt.Chart(df).mark_bar().encode(
                    x='Subject', 
                    y=alt.Y('Percentage', scale=alt.Scale(domain=[0,100])),
                    color=alt.Color('Percentage', scale=alt.Scale(domain=[0, 75, 85, 100], range=['red', 'orange', 'green', 'green']), legend=None)
                ).properties(height=300)
                st.altair_chart(c, use_container_width=True)
                st.dataframe(df, use_container_width=True)
            else:
                st.info("No data available.")

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
                        st.error("Invalid")

    user = st.session_state['auth_user']
    if user:
        if user['role'] == "Admin": admin_dashboard()
        elif user['role'] == "Faculty": faculty_dashboard(user)
    else:
        student_public_dashboard()

if __name__ == "__main__":
    main()
