
import streamlit as st
from openai import AzureOpenAI
import fitz  # PyMuPDF
from PIL import Image
import base64
from io import BytesIO
from azure.storage.blob import BlobServiceClient,generate_blob_sas, BlobSasPermissions  
import os
from datetime import datetime, timedelta
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_LEFT
import pandas as pd

st.set_page_config(page_title="PayShield - AI Audit", page_icon="🛡️", layout="centered")

# Add this at the top of your app
PASSWORD = st.secrets.get("APP_PASSWORD", "")

if "authenticated" not in st.session_state:
    st.session_state["authenticated"] = False

if not st.session_state["authenticated"]:
    pwd = st.text_input("🔒 Enter password to access PayShield", type="password")
    if pwd == PASSWORD:
        st.session_state["authenticated"] = True
    else:
        st.stop()


# === PayShield Branding ===
st.markdown("""
    <h1 style='text-align: center; color:#2c3e50;'>🛡️ PayShield</h1>
    <h4 style='text-align: center; color: #7f8c8d;'>Smart Payment Audits & Anomaly Detection for AP Teams</h4>
    <p style='text-align: center;'>Upload a PO and Invoice to validate labor rates, hours, and catch discrepancies – instantly, with AI.</p>
    """, unsafe_allow_html=True)

# === Azure OpenAI Settings ===


API_KEY = st.secrets["API_KEY"] 
ENDPOINT = "https://payshieldpoc-llm.cognitiveservices.azure.com/"
API_VERSION = "2024-12-01-preview"

client = AzureOpenAI(
    api_key=API_KEY,
    api_version=API_VERSION,
    azure_endpoint=ENDPOINT
)


# === Azure Blob Storage Settings ===
BLOB_ACCOUNT_NAME = "llmpocstorage"
BLOB_ACCOUNT_KEY = st.secrets["BLOB_ACCOUNT_KEY"]
BLOB_CONTAINER = "documents"

blob_service_client = BlobServiceClient(
    account_url=f"https://{BLOB_ACCOUNT_NAME}.blob.core.windows.net",
    credential=BLOB_ACCOUNT_KEY
)


# Initialize BlobServiceClient and ContainerClient
container_client = blob_service_client.get_container_client(BLOB_CONTAINER)


# === File Upload ===
st.markdown("### 📂 Upload Documents")
col1, col2 = st.columns(2)
with col1:
    po_file = st.file_uploader("📂 Upload PO / Agreement (PDF or Image)", type=["pdf", "jpg", "jpeg", "png"])
with col2:
    invoice_file = st.file_uploader("📂 Upload Invoice / Timesheet (PDF or Image)", type=["pdf", "jpg", "jpeg", "png"])

#def file_to_base64_and_mime(uploaded_file):
#    file_bytes = uploaded_file.read()
#    file_base64 = base64.b64encode(file_bytes).decode("utf-8")
#    file_mime = uploaded_file.type  # e.g., 'application/pdf' or 'image/png'
#    return file_base64, file_mime

# === SAS Token Generation ===
def get_blob_sas_url(blob_name):  # <-- ADDED
    sas_token = generate_blob_sas(
        account_name=BLOB_ACCOUNT_NAME,
        container_name=BLOB_CONTAINER,
        blob_name=blob_name,
        account_key=BLOB_ACCOUNT_KEY,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.utcnow() + timedelta(hours=1)
    )
    return f"https://{BLOB_ACCOUNT_NAME}.blob.core.windows.net/{BLOB_CONTAINER}/{blob_name}?{sas_token}"


def convert_pdf_to_images(pdf_file):
    pdf_file.seek(0)
    doc = fitz.open(stream=pdf_file.read(), filetype="pdf")
    images = []
    for page in doc:
        pix = page.get_pixmap(dpi=200)
        img = Image.open(BytesIO(pix.tobytes("png")))
        images.append(img)
    return images

def upload_image_to_blob(image: Image.Image, blob_name: str):
    buf = BytesIO()
    image.save(buf, format="PNG")
    buf.seek(0)
    blob_client = container_client.get_blob_client(blob_name)
    blob_client.upload_blob(buf, overwrite=True)
    return get_blob_sas_url(blob_name)  # Assume blob container has public access or SAS token

def handle_uploaded_file(uploaded_file, prefix):
    image_urls = []
    if uploaded_file.type == "application/pdf":
        images = convert_pdf_to_images(uploaded_file)
        for idx, image in enumerate(images):
            blob_name = f"{prefix}_page_{idx+1}.png"
            url = upload_image_to_blob(image, blob_name)
            image_urls.append(url)
    else:
        image = Image.open(uploaded_file).convert("RGB")
        blob_name = f"{prefix}_img.png"
        url = upload_image_to_blob(image, blob_name)
        image_urls.append(url)
    return image_urls


def generate_pdf(text, filename="PayShield_Audit.pdf"):
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4,
                            rightMargin=30, leftMargin=30,
                            topMargin=30, bottomMargin=18)
    styles = getSampleStyleSheet()
    styles["Normal"].fontSize = 10
    styles["Normal"].leading = 14
    styles["Normal"].alignment = TA_LEFT

    story = []
    for line in text.split("\n"):
        if line.strip():
            story.append(Paragraph(line.strip(), styles["Normal"]))

    doc.build(story)
    buffer.seek(0)
    return buffer

def extract_table_to_csv(result_text):
    lines = result_text.splitlines()
    table_lines = [line for line in lines if '|' in line and '---' not in line]
    if not table_lines:
        return None, None

    rows = []
    for line in table_lines:
        cols = [col.strip() for col in line.strip('|').split('|')]
        rows.append(cols)

    df = pd.DataFrame(rows[1:], columns=rows[0]) if len(rows) > 1 else pd.DataFrame(rows)
    csv = df.to_csv(index=False)
    return csv.encode('utf-8'), df


# === Prompt Template ===
prompt = """

You are acting as an expert Accounts Payable (AP) analyst conducting an audit between a Purchase Order (PO) or labor agreement and a corresponding invoice or timesheet.

Your task is to analyze both documents and determine whether the invoice aligns with the PO agreement.

Please follow these steps:

1. Extract the labor categories or roles (e.g., Engineer, Technician, Laborer) as listed under the labor section of the **invoice document only**.
2. For each role:
    - Extract both **"Straight Time"** and **"Time and One-Half"** hourly rates from the **PO or agreement**.
    - Extract the **billed rate**, and **estimated hours** from the **invoice**.
    - Determine which rate should apply:
        - If hours ≤ 8, use **Straight Time** from PO.
        - If hours > 8, use **Time and One-Half** from PO.
3. Calculate:
    - **Total Cost in Invoice**: `Invoice Billed Rate × Estimated Hours`
    - **Total Cost as per PO Rate**: Apply logic above to compute.
    - **Difference**: `Invoice Cost - PO Cost`
4. Determine **compliance status**:
    - Mark as **Compliant** if billed rate and calculated costs match the PO.
    - Mark as **Non-Compliant** otherwise, with reason in comments.

Present the results in the following tabular format, ensure to keep it as HTML table:

| Role | PO Approved Rate Card (Straight Time) | PO Approved Rate Card (Time and One-Half) | Invoice Billed Rate | Estimated Hours in Invoice | Total cost  as per Invoice rate | Total Cost as per PO Rate | Overbilled amount | Compliance Status |  Comments

End the report with a professional summary that includes:
- Total overbilled amount (sum of all overfilled amount column from above table)
- A final conclusion on whether overpayment occurred
- Any document inconsistencies observed
- Next recommended action (e.g., "Follow up with the supplier for clarification", "Hold payment until discrepancy is resolved", etc.)

Be concise, formal, and use business-appropriate language throughout.

⚠️ Do not deviate from the above structure. Output the table  first, followed by the "summary" section using the above specified bullet point format. Avoid creative variations or freeform text.

"""
st.markdown("### 🧠 Prompt Customization (Optional)")
custom_prompt = st.text_area("Edit the AI prompt below:", value=prompt, height=250)

if 'audit_result' not in st.session_state:
    st.session_state['audit_result'] = None

# === Button and Result ===
if po_file and invoice_file:
    st.success("✅ Both documents uploaded. Ready to analyze.")
    # Using a form to isolate Analyze button from download buttons:
    with st.form("analyze_form"):
        analyze_btn = st.form_submit_button("🔍 Analyze with PayShield AI")
        
    if analyze_btn:
        with st.spinner("Running audit with GPT-4o Mini..."):
            try:
                #po_filename = f"po_{uuid.uuid4()}.{file1.name.split('.')[-1]}"
                #invoice_filename = f"invoice_{uuid.uuid4()}.{file2.name.split('.')[-1]}"
                po_image_urls = handle_uploaded_file(po_file, "po")
                invoice_image_urls = handle_uploaded_file(invoice_file, "invoice")
                
                print("po_url",po_image_urls)
                print("invoice_url",invoice_image_urls)
                content_blocks = [
                    {"type": "text", "text": "Document 1: Purchase Order (PO)."}
                ] + [
                    {"type": "image_url", "image_url": {"url": url}} for url in po_image_urls
                ] + [
                    {"type": "text", "text": "Document 2: Invoice."}
                ] + [
                    {"type": "image_url", "image_url": {"url": url}} for url in invoice_image_urls
                ]

                messages = [{"role": "user", "content": [{"type": "text", "text": custom_prompt}] + content_blocks}]
                
                response = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=messages
                )

                result = response.choices[0].message.content
                st.session_state["audit_result"] = result

            except Exception as e:
                st.error(f"❌ Error: {e}")
                
    if "audit_result" in st.session_state:
        result = st.session_state["audit_result"]
        st.markdown("---")
        st.markdown("### 📊 PayShield AI Audit Summary")
        st.markdown(f"<div style='background-color:#f9f9f9;padding:15px;border-radius:8px;'>{result}</div>", unsafe_allow_html=True)
                
        st.markdown("### 📎 Download Audit")
        pdf_bytes = generate_pdf(result)
        st.download_button(
            label="📄 Download PDF Report",
            data=pdf_bytes,
            file_name="PayShield_Audit_Report.pdf",
            mime="application/pdf"
        )
                
        csv_data, df = extract_table_to_csv(result)
        if csv_data:
            st.download_button(
                label="📊 Download CSV Table",
                data=csv_data,
                file_name="PayShield_Comparison.csv",
                mime="text/csv"
            )
        else:
            st.info("🛈 No structured table found for CSV export.")
else:
    st.info("📁 Please upload both a PO and Invoice file to begin analysis.")
