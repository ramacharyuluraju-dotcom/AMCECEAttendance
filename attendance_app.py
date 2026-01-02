import streamlit as st
import pandas as pd
import firebase_admin
from firebase_admin import credentials, firestore
import datetime
import altair as alt

# ==========================================
# 1. SETUP & CONFIGURATION
# ==========================================

st.set_page_config(page_title="VTU Attendance System", page_icon="🎓", layout="wide")

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
            
    BUCKET_NAME = "your-project-id.appspot.com" # Replace if using Storage
    firebase_admin.initialize_app(cred)

db = firestore.client()

if 'auth_user' not in st.session_state:
    st.session_state['auth_user'] = None

# ==========================================
# 2. DATA PROCESSING (Updated for Batch/AY)
# ==========================================

def clean_df(df):
    """Cleans headers to be lower_case_no_spaces"""
    df.columns = [str(c).strip().lower().replace(" ", "").replace("_", "") for c in df.columns]
    return df.fillna("")

def batch_process_part_a(df):
    """
    Part A: Courses & Faculty
    Columns: AY, Dept, Sem, Section, SubCode, SubTitle, FacName, FacEmail
    """
    df = clean_df(df)
    batch = db.batch()
    count = 0
    logs = []
    
    for _, row in df.iterrows():
        # Validation
        if not row.get('subcode') or not row.get('facultyemail'):
            continue
            
        cid = f"{row['ay']}_{row['dept']}_{row['sem']}_{row['section']}_{row['subcode']}"
        
        # 1. Create Course
        batch.set(db.collection('Courses').document(cid), {
            "ay": str(row['ay']),
            "dept": str(row['dept']).upper(),
            "sem": str(row['sem']),
            "section": str(row['section']).upper(),
            "subcode": str(row['subcode']).upper(),
            "subtitle": str(row['subtitle']),
            "faculty_id": str(row['facultyemail']).lower().strip(),
            "faculty_name": str(row['facultyname'])
        })
        
        # 2. Create Faculty Login (if not exists)
        fid = str(row['facultyemail']).lower().strip()
        user_ref = db.collection('Users').document(fid)
        # We perform a quick read to avoid overwriting existing passwords
        # But in bulk upload, we ensure the user exists.
        batch.set(user_ref, {
            "name": row['facultyname'],
            "role": "Faculty",
            "dept": str(row['dept']).upper(),
            # Only set default password if creating new
            "password": "password123" 
        }, merge=True)
        
        logs.append(f"Linked {row['subcode']} to {row['facultyname']}")
        
        count += 1
        if count % 200 == 0:
            batch.commit()
            batch = db.batch()
            
    batch.commit()
    return count, logs

def batch_process_part_b(df):
    """
    Part B: Students
    Columns: USN, Name, Dept, Sem, Section, Batch (New!)
    """
    df = clean_df(df)
    batch = db.batch()
    count = 0
    
    # Pre-fetch courses to optimize linking
    # Map: "CS_5_A" -> [{'code': '18CS51', 'title': 'DBMS'}]
    course_map = {}
    all_courses = db.collection('Courses').stream()
    for c in all_courses:
        d = c.to_dict()
        key = f"{d['dept']}_{d['sem']}_{d['section']}"
        if key not in course_map: course_map[key] = []
        course_map[key].append(d)
        
    for _, row in df.iterrows():
        if not row.get('usn'): continue
        
        usn = str(row['usn']).upper().strip()
        dept = str(row['dept']).upper().strip()
        sem = str(row['sem']).strip()
        sec = str(row['section']).upper().strip()
        
        # 1. Create Student Profile
        batch.set(db.collection('Students').document(usn), {
            "name": row['name'],
            "dept": dept,
            "sem": sem,
            "section": sec,
            "batch": str(row.get('batch', '')) # Added Batch
        })
        
        # 2. Auto-Link Subjects
        key = f"{dept}_{sem}_{sec}"
        if key in course_map:
            summ_ref = db.collection('Student_Summaries').document(usn)
            updates = {}
            for subj in course_map[key]:
                # Initialize stats if not present
                code = subj['subcode']
                updates[f"{code}.total"] = 0
                updates[f"{code}.attended"] = 0
                updates[f"{code}.title"] = subj['subtitle']
            
            # Using merge=True to safely add new subjects without erasing old ones
            batch.set(summ_ref, updates, merge=True)
            
        count += 1
        if count % 200 == 0:
            batch.commit()
            batch = db.batch()
            
    batch.commit()
    return count

# ==========================================
# 3. ADMIN TABS (NEW!)
# ==========================================

def tab_bulk_upload():
    c1, c2 = st.columns(2)
    
    with c1:
        st.subheader("📤 1. Upload Courses (Part A)")
        st.info("Required: AY, Dept, Sem, Section, SubCode, SubTitle, FacultyName, FacultyEmail")
        f1 = st.file_uploader("Courses CSV", type='csv', key='a')
        if f1 and st.button("Process Courses"):
            try:
                c, logs = batch_process_part_a(pd.read_csv(f1))
                st.success(f"✅ Processed {c} records.")
                with st.expander("View Logs"):
                    st.write(logs)
            except Exception as e:
                st.error(f"Error: {e}")

    with c2:
        st.subheader("📤 2. Upload Students (Part B)")
        st.info("Required: USN, Name, Dept, Sem, Section, Batch")
        f2 = st.file_uploader("Students CSV", type='csv', key='b')
        if f2 and st.button("Process Students"):
            try:
                c = batch_process_part_b(pd.read_csv(f2))
                st.success(f"✅ Registered {c} students & linked subjects.")
            except Exception as e:
                st.error(f"Error: {e}")

def tab_manage_faculty():
    st.subheader("👨‍🏫 Manage Faculty Logins")
    
    # 1. Manual Creation
    with st.expander("➕ Add New Faculty Manually"):
        with st.form("add_fac"):
            c1, c2, c3 = st.columns(3)
            new_email = c1.text_input("Email (Login ID)").strip().lower()
            new_name = c2.text_input("Full Name")
            new_dept = c3.text_input("Dept (e.g. CS)")
            if st.form_submit_button("Create Login"):
                if new_email and new_name:
                    db.collection('Users').document(new_email).set({
                        "name": new_name, "password": "password123", 
                        "role": "Faculty", "dept": new_dept
                    })
                    st.success(f"Created user for {new_name}")
                else:
                    st.error("Email and Name are required.")

    # 2. View List
    st.divider()
    st.write("### Existing Faculty List")
    if st.button("🔄 Refresh List"):
        docs = db.collection('Users').where("role", "==", "Faculty").stream()
        data = [{"Email": d.id, **d.to_dict()} for d in docs]
        if data:
            st.dataframe(pd.DataFrame(data), use_container_width=True)
        else:
            st.info("No faculty found.")

def tab_manage_students():
    st.subheader("🎓 Manage Students")
    
    # 1. Search/Edit Single
    with st.container():
        c1, c2 = st.columns([1, 3])
        search_usn = c1.text_input("Enter USN to Edit").strip().upper()
        if c1.button("Search USN"):
            doc = db.collection('Students').document(search_usn).get()
            if doc.exists:
                d = doc.to_dict()
                with c2.form("edit_stu"):
                    st.write(f"Editing: **{search_usn}**")
                    en = st.text_input("Name", value=d.get('name', ''))
                    ed = st.text_input("Dept", value=d.get('dept', ''))
                    es = st.text_input("Sem", value=d.get('sem', ''))
                    esec = st.text_input("Section", value=d.get('section', ''))
                    ebatch = st.text_input("Batch", value=d.get('batch', ''))
                    
                    if st.form_submit_button("Update Student"):
                        db.collection('Students').document(search_usn).update({
                            "name": en, "dept": ed, "sem": es, 
                            "section": esec, "batch": ebatch
                        })
                        st.success("Updated!")
            else:
                c2.error("Student not found.")

def tab_manage_courses():
    st.subheader("📚 Manage Subjects (SubCodes)")
    
    # Filter to avoid loading 1000s of rows
    col1, col2 = st.columns(2)
    f_dept = col1.text_input("Filter by Dept", "CS")
    f_sem = col2.text_input("Filter by Sem", "5")
    
    if st.button("Load Courses"):
        docs = db.collection('Courses')\
            .where("dept", "==", f_dept)\
            .where("sem", "==", f_sem).stream()
            
        data = []
        for d in docs:
            dd = d.to_dict()
            dd['id'] = d.id # capture doc ID for updates
            data.append(dd)
            
        if data:
            st.write(f"Found {len(data)} courses.")
            # Display as a table
            df = pd.DataFrame(data)
            st.dataframe(df[['subcode', 'subtitle', 'section', 'faculty_name', 'faculty_id']])
            
            st.info("To edit, use the form below with the specific SubCode and Section.")
            
            with st.form("edit_course_form"):
                st.write("#### Edit Specific Course")
                ec_code = st.text_input("SubCode (e.g. 18CS51)").strip().upper()
                ec_sec = st.text_input("Section (e.g. A)").strip().upper()
                
                new_title = st.text_input("New Title (Optional)")
                new_fac_email = st.text_input("New Faculty Email (Optional)")
                
                if st.form_submit_button("Update Course"):
                    # We need to reconstruct the ID to find it
                    # ID format: AY_Dept_Sem_Sec_SubCode
                    # This is tricky because we don't know AY here easily.
                    # Alternative: Query by fields
                    q = db.collection('Courses')\
                        .where("subcode", "==", ec_code)\
                        .where("section", "==", ec_sec)\
                        .where("dept", "==", f_dept).stream()
                    
                    found = False
                    for qd in q:
                        found = True
                        upd = {}
                        if new_title: upd['subtitle'] = new_title
                        if new_fac_email: 
                            upd['faculty_id'] = new_fac_email
                            # Optional: fetch faculty name for consistency
                            fdoc = db.collection('Users').document(new_fac_email).get()
                            if fdoc.exists:
                                upd['faculty_name'] = fdoc.to_dict()['name']
                        
                        db.collection('Courses').document(qd.id).update(upd)
                    
                    if found: st.success("Course updated!")
                    else: st.error("Course not found with those details.")
        else:
            st.warning("No courses found for this filter.")

# ==========================================
# 4. VIEW CONTROLLERS
# ==========================================

def admin_dashboard():
    st.title("⚙️ Super Admin Dashboard")
    
    tabs = st.tabs([
        "📤 Bulk Uploads", 
        "👨‍🏫 Manage Faculty", 
        "🎓 Manage Students", 
        "📚 Manage Courses"
    ])
    
    with tabs[0]: tab_bulk_upload()
    with tabs[1]: tab_manage_faculty()
    with tabs[2]: tab_manage_students()
    with tabs[3]: tab_manage_courses()

def faculty_dashboard(user):
    st.title(f"👨‍🏫 Welcome, {user['name']}")
    
    # 1. Fetch Courses
    courses = list(db.collection('Courses').where("faculty_id", "==", user['id']).stream())
    
    if not courses:
        st.warning("No courses assigned to you.")
        return

    # Dropdown
    c_map = {f"{c.to_dict()['subcode']} ({c.to_dict()['section']})" : c.to_dict() for c in courses}
    sel = st.selectbox("Select Class to Mark", list(c_map.keys()))
    course = c_map[sel]
    
    st.divider()
    
    # 2. Fetch Students
    students = db.collection('Students')\
        .where("dept", "==", course['dept'])\
        .where("sem", "==", course['sem'])\
        .where("section", "==", course['section']).stream()
        
    s_list = sorted([{"usn": d.id, "name": d.to_dict()['name']} for d in students], key=lambda x: x['usn'])
    
    if not s_list:
        st.error("No students found in this section.")
        return

    # 3. Mark Attendance
    with st.form("attendance_form"):
        c1, c2 = st.columns([1, 2])
        date_val = c1.date_input("Date", datetime.date.today())
        # Editable Faculty Name for Substitutes
        fac_name = c2.text_input("Faculty Handling Class", value=course['faculty_name'])
        
        st.write("### Student List")
        status = {}
        cols = st.columns(4)
        for i, s in enumerate(s_list):
            status[s['usn']] = cols[i%4].checkbox(s['usn'], value=True)
            
        if st.form_submit_button("Submit Attendance"):
            absentees = [k for k,v in status.items() if not v]
            
            batch = db.batch()
            
            # Log Session
            batch.set(db.collection('Class_Sessions').document(), {
                "course_code": course['subcode'],
                "section": course['section'],
                "date": str(date_val),
                "faculty_name": fac_name,
                "absentees": absentees,
                "timestamp": datetime.datetime.now()
            })
            
            # Update Totals
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
            st.balloons()
            st.success("Attendance Saved Successfully!")

def student_public_dashboard():
    st.markdown("<h1 style='text-align: center;'>🎓 Student Attendance Portal</h1>", unsafe_allow_html=True)
    
    col1, col2, col3 = st.columns([1,2,1])
    with col2:
        usn = st.text_input("Enter USN (e.g. 1MV20CS001)").strip().upper()
        if st.button("Check Status", use_container_width=True):
            if not usn:
                st.warning("Please enter a USN.")
                return
            
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
                    pct = (att/tot*100) if tot > 0 else 0.0
                    status = "Safe" if pct >= 85 else ("Warning" if pct >= 75 else "Critical")
                    rows.append({
                        "Subject": sub, "Title": stats.get('title', sub),
                        "Percentage": pct, "Status": status, "Classes": f"{att}/{tot}"
                    })
            
            if rows:
                df = pd.DataFrame(rows)
                
                # Metrics
                m1, m2, m3 = st.columns(3)
                m1.metric("Avg Attendance", f"{df['Percentage'].mean():.1f}%")
                m2.metric("Safe Subjects", len(df[df['Status']=='Safe']))
                m3.metric("Critical", len(df[df['Status']=='Critical']))
                
                st.divider()
                
                # Chart
                c = alt.Chart(df).mark_bar().encode(
                    x='Subject', 
                    y=alt.Y('Percentage', scale=alt.Scale(domain=[0,100])),
                    color=alt.Color('Percentage', scale=alt.Scale(domain=[0, 75, 85, 100], range=['red', 'orange', 'green', 'green']), legend=None)
                ).properties(height=300)
                
                st.altair_chart(c, use_container_width=True)
                st.dataframe(df[['Subject', 'Title', 'Classes', 'Percentage', 'Status']], use_container_width=True)
            else:
                st.info("No attendance records found yet.")

# ==========================================
# 5. MAIN ROUTER
# ==========================================

def main():
    # Sidebar Login
    with st.sidebar:
        st.header("🔐 Staff Login")
        if st.session_state['auth_user']:
            st.success(f"User: {st.session_state['auth_user']['name']}")
            if st.button("Logout"):
                st.session_state['auth_user'] = None
                st.rerun()
        else:
            uid = st.text_input("Email / Admin ID")
            pwd = st.text_input("Password", type="password")
            if st.button("Login"):
                if uid == "admin" and pwd == "admin123":
                    st.session_state['auth_user'] = {"id": "admin", "name": "Administrator", "role": "Admin"}
                    st.rerun()
                else:
                    # Check Firestore Users
                    doc = db.collection('Users').document(uid).get()
                    if doc.exists and doc.to_dict().get('password') == pwd:
                        u = doc.to_dict()
                        u['id'] = uid
                        st.session_state['auth_user'] = u
                        st.rerun()
                    else:
                        st.error("Invalid Credentials")

    # Routing
    user = st.session_state['auth_user']
    
    if user:
        if user['role'] == "Admin":
            admin_dashboard()
        elif user['role'] == "Faculty":
            faculty_dashboard(user)
    else:
        student_public_dashboard()

if __name__ == "__main__":
    main()
