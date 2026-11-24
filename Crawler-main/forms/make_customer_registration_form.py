"""AVIGIRI Customer Registration Form: printable and fillable A4 PDF."""
import re
import sys

from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

LOGO, OUT = sys.argv[1], sys.argv[2]

NAVY = HexColor("#24238C")
NAVY_DARK = HexColor("#1A1A66")
LIME = HexColor("#B5CC2E")
INK = HexColor("#1F2330")
MUTED = HexColor("#5B6275")
LINE = HexColor("#C9CEDD")
FIELD = HexColor("#F4F6FB")
TINT = HexColor("#E9ECF7")

W, H = A4
M = 34                      # side margin
CW = W - 2 * M              # content width
GAP = 8                     # gap between cells
LABEL_H = 11                # label line above a box
ROW_GAP = 7
TOP_LIMIT = H - 112         # content starts below the header
BOTTOM_LIMIT = 62           # content stops above the footer
TOTAL_PAGES = 2

c = canvas.Canvas(OUT, pagesize=A4)
c.setTitle("AVIGIRI - Customer Registration Form")
c.setAuthor("AVIGIRI Urethane & Rubber Industries Pvt. Ltd.")
c.setSubject("Customer Registration / Know Your Customer (KYC) Form")
form = c.acroForm
_names = {}
page_no = 1
y = 0


def fname(label):
    base = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:40] or "field"
    _names[base] = _names.get(base, 0) + 1
    return base if _names[base] == 1 else f"{base}_{_names[base]}"


# ---------------------------------------------------------------- page chrome
def header():
    top = H - 30
    c.drawImage(ImageReader(LOGO), M - 4, top - 50, width=88, height=52, mask="auto",
                preserveAspectRatio=True, anchor="sw")
    c.setFillColor(NAVY)
    c.setFont("Helvetica-Bold", 15.5)
    c.drawString(M + 92, top - 14, "AVIGIRI Urethane &")
    c.drawString(M + 92, top - 32, "Rubber Industries Pvt. Ltd.")
    c.setFont("Helvetica-Bold", 8)
    c.drawString(M + 92, top - 47, "www.avigiri.com")
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 7.5)
    c.drawString(M + 166, top - 47, "CIN : U25203KA2011PTC059765")

    # rule: navy with lime accent
    c.setFillColor(NAVY)
    c.rect(M, top - 62, CW, 3, stroke=0, fill=1)
    c.setFillColor(LIME)
    c.rect(M, top - 62, 90, 3, stroke=0, fill=1)


def footer():
    c.setFillColor(NAVY)
    c.rect(M, 44, CW, 1.6, stroke=0, fill=1)
    c.setFillColor(MUTED)
    c.setFont("Helvetica-Oblique", 7)
    c.drawCentredString(W / 2, 33, "169D Vasanthanarasapura Industrial Area, Kora Hobli, Tumakuru District, "
                                   "TUMAKURU - 572106, Karnataka, India.")
    c.drawCentredString(W / 2, 23, "Tel: +91 816 297 00 27 / 25   |   Email: apai@avigiri.com   |   www.avigiri.com")
    c.setFont("Helvetica-Bold", 7)
    c.setFillColor(NAVY)
    c.drawRightString(W - M, 33, f"Page {page_no} of {TOTAL_PAGES}")


def new_page():
    global page_no, y
    footer()
    c.showPage()
    page_no += 1
    header()
    y = TOP_LIMIT


# ---------------------------------------------------------------- building blocks
def section(num, title, note=""):
    global y
    y -= 6
    h = 19
    c.setFillColor(NAVY)
    c.roundRect(M, y - h, CW, h, 3, stroke=0, fill=1)
    c.setFillColor(LIME)
    c.roundRect(M, y - h, 24, h, 3, stroke=0, fill=1)
    c.rect(M + 20, y - h, 4, h, stroke=0, fill=1)
    c.setFillColor(NAVY_DARK)
    c.setFont("Helvetica-Bold", 9.5)
    c.drawCentredString(M + 12, y - 13, str(num))
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 9.5)
    c.drawString(M + 32, y - 13, title.upper())
    if note:
        c.setFont("Helvetica-Oblique", 7.2)
        c.setFillColor(HexColor("#D5D9F2"))
        c.drawRightString(W - M - 8, y - 12.6, note)
    y -= h + 8


def label(x, ytop, text, required=False):
    c.setFillColor(MUTED)
    c.setFont("Helvetica-Bold", 6.8)
    c.drawString(x, ytop - 8, text.upper())
    if required:
        c.setFillColor(HexColor("#C0392B"))
        c.drawString(x + c.stringWidth(text.upper(), "Helvetica-Bold", 6.8) + 1.5, ytop - 8, "*")


def text_box(x, ybot, w, h, name, multiline=False):
    c.setFillColor(FIELD)
    c.setStrokeColor(LINE)
    c.setLineWidth(0.6)
    c.roundRect(x, ybot, w, h, 2, stroke=1, fill=1)
    form.textfield(name=name, x=x + 1.5, y=ybot + 1, width=w - 3, height=h - 2, borderWidth=0,
                   fillColor=FIELD, borderColor=FIELD, textColor=INK, fontName="Helvetica",
                   fontSize=0 if multiline else 9, fieldFlags="multiline" if multiline else "",
                   tooltip=name.replace("_", " "))


def check(x, ybot, name, text, size=9):
    form.checkbox(name=name, x=x, y=ybot, size=size, buttonStyle="check", borderColor=NAVY,
                  fillColor=white, textColor=NAVY, borderWidth=0.8, forceBorder=True,
                  tooltip=text)
    c.setFillColor(INK)
    c.setFont("Helvetica", 8)
    c.drawString(x + size + 4, ybot + 1.5, text)
    return x + size + 4 + c.stringWidth(text, "Helvetica", 8) + 14


def row(cells, h=20):
    """cells: (label, fraction, kind, extra). kind: text | multi | choice (extra=options) | blank."""
    global y
    total_gap = GAP * (len(cells) - 1)
    x = M
    for lab, frac, kind, *extra in cells:
        w = (CW - total_gap) * frac
        req = lab.endswith("*")
        lab = lab.rstrip("*").strip()
        label(x, y, lab, req)
        ybot = y - LABEL_H - h
        if kind in ("text", "multi"):
            text_box(x, ybot, w, h, fname(lab), multiline=(kind == "multi"))
        elif kind == "choice":
            c.setStrokeColor(LINE)
            c.setLineWidth(0.6)
            c.setFillColor(white)
            c.roundRect(x, ybot, w, h, 2, stroke=1, fill=1)
            cx = x + 7
            base = fname(lab)
            for opt in extra[0]:
                cx = check(cx, ybot + (h - 9) / 2, f"{base}__{re.sub(r'[^a-z0-9]+', '_', opt.lower())}", opt)
        x += w + GAP
    y -= LABEL_H + h + ROW_GAP


def same_as(text):
    global y
    check(W - M - 9 - 4 - c.stringWidth(text, "Helvetica", 8) - 2, y + 2, fname(text), text)


def address_block(title, same_text=None, required=True):
    global y
    star = "*" if required else ""
    if same_text:
        c.setFillColor(MUTED)
        c.setFont("Helvetica-Bold", 6.8)
        form.checkbox(name=fname(same_text), x=W - M - 132, y=y - 9.5, size=8.5, buttonStyle="check",
                      borderColor=NAVY, fillColor=white, textColor=NAVY, borderWidth=0.8, forceBorder=True,
                      tooltip=same_text)
        c.setFillColor(NAVY)
        c.setFont("Helvetica-Bold", 7.2)
        c.drawString(W - M - 120, y - 8, same_text)
    row([(title + star, 1, "multi")], h=30)
    row([("City / Town" + star, .32, "text"), ("District", .24, "text"), ("State" + star, .24, "text"),
         ("PIN Code" + star, .20, "text")])


def table(title_cols, nrows, row_h=19):
    """title_cols: list of (heading, fraction)."""
    global y
    hh = 16
    x = M
    widths = [CW * f for _, f in title_cols]
    c.setFillColor(TINT)
    c.setStrokeColor(LINE)
    c.setLineWidth(0.6)
    c.rect(M, y - hh, CW, hh, stroke=1, fill=1)
    for (head, _), w in zip(title_cols, widths):
        c.setFillColor(NAVY)
        c.setFont("Helvetica-Bold", 7)
        c.drawString(x + 5, y - 10.5, head.upper())
        x += w
    y -= hh
    for r in range(nrows):
        x = M
        for i, ((head, _), w) in enumerate(zip(title_cols, widths)):
            if i == 0 and isinstance(head, str) and head.upper() in ("DEPARTMENT", "NO."):
                c.setFillColor(white)
                c.rect(x, y - row_h, w, row_h, stroke=1, fill=1)
                c.setFillColor(INK)
                c.setFont("Helvetica-Bold", 7.6)
                c.drawString(x + 5, y - row_h / 2 - 2.6, TABLE_FIRST_COL[r])
            else:
                c.setFillColor(FIELD)
                c.rect(x, y - row_h, w, row_h, stroke=1, fill=1)
                form.textfield(name=f"contact_{r + 1}_{fname(head)}", x=x + 1.5, y=y - row_h + 1,
                               width=w - 3, height=row_h - 2, borderWidth=0, fillColor=FIELD,
                               borderColor=FIELD, textColor=INK, fontName="Helvetica", fontSize=8.5,
                               tooltip=f"{TABLE_FIRST_COL[r]} - {head}")
            x += w
        y -= row_h
    y -= ROW_GAP + 4


def paragraph(text, size=7.8, leading=10.5, color=INK, font="Helvetica", width=CW, x=M):
    global y
    c.setFillColor(color)
    c.setFont(font, size)
    words, line = text.split(), ""
    for wd in words:
        trial = (line + " " + wd).strip()
        if c.stringWidth(trial, font, size) > width:
            c.drawString(x, y - size, line)
            y -= leading
            line = wd
        else:
            line = trial
    if line:
        c.drawString(x, y - size, line)
        y -= leading


# ---------------------------------------------------------------- "same as" address checkboxes
ADDRESS_JS = """
var AVG_COMPANY = ["registered_company_address", "city_town", "district", "state", "pin_code"];
var AVG_BILLING = ["billing_address_buyer", "city_town_2", "district_2", "state_2", "pin_code_2"];
var AVG_DELIVERY = ["delivery_address_consignee", "city_town_3", "district_3", "state_3", "pin_code_3"];
function avgChecked(doc, name) { return doc.getField(name).value !== "Off"; }
function avgCopy(doc, from, to) {
  for (var i = 0; i < from.length; i++) { var f = doc.getField(to[i]); f.value = doc.getField(from[i]).value; f.readonly = true; }
}
function avgRelease(doc, to) {
  for (var i = 0; i < to.length; i++) { var f = doc.getField(to[i]); f.readonly = false; f.value = ""; }
}
function avgRefresh(doc) {
  if (avgChecked(doc, "same_as_company_address")) avgCopy(doc, AVG_COMPANY, AVG_BILLING);
  if (avgChecked(doc, "same_as_billing_address")) avgCopy(doc, AVG_BILLING, AVG_DELIVERY);
}
function avgToggleBilling(doc) {
  if (!avgChecked(doc, "same_as_company_address")) avgRelease(doc, AVG_BILLING);
  avgRefresh(doc);
}
function avgToggleDelivery(doc) {
  if (!avgChecked(doc, "same_as_billing_address")) avgRelease(doc, AVG_DELIVERY);
  avgRefresh(doc);
}
"""


def add_address_copy(path):
    """Ticking a "Same as" box copies the address and keeps it in step with its source."""
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, TextStringObject

    def js(code):
        return DictionaryObject({NameObject("/S"): NameObject("/JavaScript"),
                                 NameObject("/JS"): TextStringObject(code)})

    sources = {"registered_company_address", "city_town", "district", "state", "pin_code",
               "billing_address_buyer", "city_town_2", "district_2", "state_2", "pin_code_2"}
    toggles = {"same_as_company_address": "avgToggleBilling(this);", "same_as_billing_address": "avgToggleDelivery(this);"}
    writer = PdfWriter(clone_from=PdfReader(path))
    found = set()
    for page in writer.pages:
        for ref in page.get("/Annots", []):
            annot = ref.get_object()
            name = str(annot.get("/T", ""))
            if name in toggles:
                annot[NameObject("/A")] = js(toggles[name])
                found.add(name)
            elif name in sources:
                annot[NameObject("/AA")] = DictionaryObject({NameObject("/Bl"): js("avgRefresh(this);")})
                found.add(name)
    missing = (sources | set(toggles)) - found
    if missing:
        raise SystemExit(f"address fields not found: {sorted(missing)}")
    writer.add_js(ADDRESS_JS)
    writer.write(path)


# ================================================================ PAGE 1
header()
y = TOP_LIMIT + 22

# title band
c.setFillColor(NAVY)
c.setFont("Helvetica-Bold", 17)
c.drawString(M, y - 18, "Customer Registration Form")
c.setFillColor(MUTED)
c.setFont("Helvetica", 8.5)
c.drawString(M, y - 31, "Know Your Customer (KYC)  -  fill in BLOCK LETTERS or type into this PDF.")
c.setFont("Helvetica-Oblique", 7)
c.setFillColor(HexColor("#C0392B"))
c.drawString(M, y - 42, "* Mandatory fields")
# office reference chip
bx, bw = W - M - 196, 196
c.setFillColor(TINT)
c.setStrokeColor(LINE)
c.roundRect(bx, y - 42, bw, 38, 3, stroke=1, fill=1)
c.setFillColor(NAVY)
c.setFont("Helvetica-Bold", 6.6)
c.drawString(bx + 7, y - 13, "CUSTOMER CODE (OFFICE USE)")
c.drawString(bx + 7, y - 31, "DATE OF REGISTRATION")
for yy, nm in ((y - 19, "customer_code_top"), (y - 37, "registration_date_top")):
    c.setFillColor(white)
    c.rect(bx + 116, yy, 74, 12, stroke=1, fill=1)
    form.textfield(name=nm, x=bx + 117, y=yy + .5, width=72, height=11, borderWidth=0, fillColor=white,
                   borderColor=white, textColor=INK, fontName="Helvetica", fontSize=7.5)
y -= 54

section(1, "Company Details")
row([("Name of the Company / Firm (as per GST)*", 1, "text")])
row([("Nature of Company*", 1, "choice",
      ["Proprietorship", "Partnership", "LLP", "Private Ltd.", "Public Ltd.", "Govt. / PSU"])])
row([("Nature of Business*", 1, "choice",
      ["OEM / Manufacturer", "Dealer / Distributor", "Trader", "End User", "Exporter", "Others"])])
row([("Year of Establishment", .22, "text"), ("CIN / Registration No.", .43, "text"),
     ("Website", .35, "text")])

section(2, "Addresses", "Name of the company is printed on every address")
address_block("Registered / Company Address")
address_block("Billing Address / Buyer", same_text="Same as Company Address")
address_block("Delivery Address / Consignee", same_text="Same as Billing Address")

section(3, "Statutory & Tax Details")
row([("GSTIN No.*", .40, "text"), ("PAN No.*", .30, "text"), ("GST State Code", .30, "text")])
row([("MSME Applicable*", .30, "choice", ["Yes", "No"]), ("Udyam Registration No.", .40, "text"),
     ("MSME Category", .30, "choice", ["Micro", "Small", "Medium"])])
row([("ISO / IATF / Other Certification", .30, "choice", ["Yes", "No"]),
     ("If Yes, Certificate No. / Standard", .70, "text")])

new_page()
y = TOP_LIMIT + 18

# ================================================================ PAGE 2
section(4, "Contact Details", "Purchase contact is mandatory")
TABLE_FIRST_COL = ["Managing Director", "Purchase *", "Design / Technical", "Accounts / Payments", "Quality",
                   "Stores / Logistics"]
table([("Department", .19), ("Name", .21), ("Designation", .16), ("Email ID", .26), ("Phone / Mobile", .18)], 6, row_h=16)

section(5, "Commercial & Logistics")
row([("Transportation*", .5, "choice", ["To Pay", "Paid", "Ex-Works / Self Pick-up"]),
     ("Delivery Type*", .5, "choice", ["Door Delivery", "Go-Down Delivery"])])
row([("Preferred Transporter (Default: ARC)", .32, "text"), ("Products / Applications of Interest", .40, "text"),
     ("Expected Annual Business (Rs. Lacs)", .28, "text")])
row([("Special Requirements (packing, labelling, documents, test certificates, delivery timings, etc.)", 1, "multi")],
    h=26)

section(6, "Bank Details", "For refunds and payment reconciliation")
row([("Bank Name & Branch", .40, "text"), ("Account No.", .34, "text"), ("IFSC Code", .26, "text")])

section(7, "Documents to be Enclosed", "Self-attested copies")
docs = ["GST Registration Certificate *", "PAN Card of the Company *", "Udyam (MSME) Certificate, if applicable",
        "Cancelled Cheque / Bank Letter", "Certificate of Incorporation / Partnership Deed",
        "ISO / IATF Certificate, if applicable"]
for i, d in enumerate(docs):
    col, r = i % 2, i // 2
    check(M + 6 + col * (CW / 2), y - 10 - r * 15, f"doc_{fname(d)}", d)
y -= 3 * 15 + 6

section(8, "Declaration")
paragraph("We hereby declare that the information given above is true and correct in all respects. We undertake to "
          "inform AVIGIRI Urethane & Rubber Industries Pvt. Ltd. in writing of any change in the above particulars, "
          "and agree that supplies will be invoiced and dispatched as per the details registered here.")
y -= 6
sig_top = y
row([("Place", .26, "text"), ("Date", .20, "text"), ("Name of Authorised Signatory", .54, "text")])
# signature & seal box on the right of a second line
label(M, y, "Designation")
text_box(M, y - LABEL_H - 20, (CW - GAP) * .46, 20, "signatory_designation")
sx = M + (CW - GAP) * .46 + GAP
sw = CW - (sx - M)
label(sx, y, "Signature of Authorised Signatory with Company Seal")
c.setStrokeColor(NAVY)
c.setDash(3, 2)
c.setLineWidth(0.8)
c.setFillColor(white)
c.roundRect(sx, y - LABEL_H - 44, sw, 44, 3, stroke=1, fill=1)
c.setDash()
y -= LABEL_H + 44 + ROW_GAP + 2

# office use panel
oh = 58
c.setFillColor(TINT)
c.setStrokeColor(NAVY)
c.setLineWidth(0.8)
c.roundRect(M, y - oh, CW, oh, 3, stroke=1, fill=1)
c.setFillColor(NAVY)
c.setFont("Helvetica-Bold", 7.5)
c.drawString(M + 8, y - 11, "FOR AVIGIRI OFFICE USE ONLY")
c.setFillColor(LIME)
c.rect(M + 8, y - 14.5, 122, 1.4, stroke=0, fill=1)
cols = [("Date of Approval", .17), ("Payment Terms", .22), ("Credit Limit (Rs.)", .17), ("Verified by (Accounts)", .22),
        ("Approved by", .22)]
x, inner = M + 8, CW - 16 - GAP * 4
for lab, f in cols:
    w = inner * f
    c.setFillColor(MUTED)
    c.setFont("Helvetica-Bold", 6.4)
    c.drawString(x, y - 26, lab.upper())
    c.setFillColor(white)
    c.setStrokeColor(LINE)
    c.setLineWidth(0.6)
    c.roundRect(x, y - 50, w, 19, 2, stroke=1, fill=1)
    form.textfield(name="office_" + fname(lab), x=x + 1.5, y=y - 49, width=w - 3, height=17, borderWidth=0,
                   fillColor=white, borderColor=white, textColor=INK, fontName="Helvetica", fontSize=8.5)
    x += w + GAP
y -= oh

if y < BOTTOM_LIMIT:
    print("WARNING: page 2 overflows by", BOTTOM_LIMIT - y)
footer()
c.save()
add_address_copy(OUT)
print("ok, page 2 bottom y =", round(y, 1))
