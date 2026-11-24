# Master prompt: KYC link + Customer Registration Form (PDF), online and manual filling

You are working in the MagicWand Django project (`D:\Crawler-main`, app `kyc`). Build, or finish, the KYC link
feature so AVIGIRI Urethane & Rubber Industries Pvt. Ltd. can collect customer KYC details through a secure
link or by staff filling them in manually. Every submission must be stored as a PDF in AVIGIRI's
Customer Registration Form layout. Read the existing code first and extend it; do not duplicate what is there.

## 1. Existing pieces to reuse (read these first)
- `kyc/models.py`: `Customer`, `CustomerContact` (categories: top_management, technical, accounts,
  sales_marketing; `is_purchaser`), `KycRequest` (status, `source` = customer/staff, `payment_terms`,
  `submitted_at`, `reviewed_by`, `reviewed_at`), `KycDetails` (one per request; PAN and account number encrypted,
  `pan_last4`, `account_last4`, `extra_contacts` JSON), `KycRequestDocument` (gst_certificate, pan_card,
  cancelled_cheque, signed_form), `KycOtp`, `KycRequestAuditLog`.
- `kyc/link_fields.py`: field list, steps, validation (GSTIN, PAN, IFSC, phone, emails, Udyam, no Aadhaar).
- `kyc/link_requests.py`: service layer (create link, OTP, save_draft, submit, review approve/reject,
  set_payment_terms, documents, `details_for`).
- `kyc/link_security.py`, `kyc/link_mail.py`, `kyc/link_views.py`, `kyc/link_public_views.py`,
  `kyc/templates/kyc/link/`, `kyc/management/commands/kyc_link_jobs.py` (reminders and expiry).
- `kyc/link_pdf.py`: renders the Customer Registration Form with fpdf2 (`request_sheet`, `customer_sheet`,
  `render_sheet`, `render`, `render_customer`).
- `forms/Avigiri_Customer_Registration_Form.pdf`: the approved blank layout. The PDF must match it.

## 2. Customer Registration Form layout (must match the blank form)
Header: Avigiri logo (`static/images/avigiri-logo.jpg`), "AVIGIRI Urethane & Rubber Industries Pvt. Ltd.",
www.avigiri.com, CIN U25203KA2011PTC059765, navy rule with lime accent. No IATF/ISO badges and no document-control
box. Footer: 169D Vasanthanarasapura Industrial Area, Kora Hobli, Tumakuru District, TUMAKURU - 572106, Karnataka,
India | Tel: +91 816 297 00 27 / 25 | apai@avigiri.com | "Page x of y". Colours: navy #24238C, lime #B5CC2E.
Title "Customer Registration Form" / "Know Your Customer (KYC)", with Customer Code and Date of Registration at
top right. Sections:
1. Company Details: company name (as per GST)*, nature of company (Proprietorship, Partnership, LLP, Private Ltd.,
   Public Ltd., Govt./PSU)*, nature of business (OEM/Manufacturer, Dealer/Distributor, Trader, End User,
   Exporter, Others)*, year of establishment, CIN/registration no., website.
2. Addresses: Registered/Company, Billing/Buyer ("Same as Company Address" box), Delivery/Consignee
   ("Same as Billing Address" box); each with city*, district, state*, PIN*. Ticking a "Same as" box copies the
   address.
3. Statutory & Tax: GSTIN*, PAN*, GST state code (first 2 digits of GSTIN), MSME Yes/No*, Udyam no.,
   MSME category (Micro/Small/Medium), ISO/IATF/other certification Yes/No + certificate no.
4. Contact Details table: Managing Director, Purchase* (mandatory), Design / Technical, Accounts / Payments,
   Quality, Stores / Logistics. Columns: name, designation, email, phone/mobile.
5. Commercial & Logistics: transportation*, delivery type (Door / Go-Down)*, preferred transporter
   (default ARC), products/applications of interest, expected annual business (Rs. lacs), special requirements.
6. Bank Details: bank name & branch, account no., IFSC.
7. Documents to be enclosed: GST certificate*, PAN card*, Udyam certificate, cancelled cheque,
   certificate of incorporation/partnership deed, ISO/IATF certificate (ticked when uploaded).
8. Declaration text, place, date, name and designation of authorised signatory, signature-with-seal box.
Office-use panel: date of approval, payment terms, credit limit, verified by (accounts), approved by.

## 3. What to build
A. Form fields. Add the fields the form has but the KYC does not collect yet: nature of company, nature of
   business, year of establishment, CIN, website, city/district/state/PIN for each of the 3 addresses, MSME
   category, certification yes/no + number, products of interest, expected annual business, MD / quality /
   stores contacts (extend contact departments with quality and stores_logistics), credit limit (staff only).
   Add them to `KycDetails` with a new migration (next number after the latest in `kyc/migrations`), to
   `link_fields.FIELDS` with validation, and to the online form steps. "Same as" ticks copy the address in the
   browser and are saved as booleans.

B. Online link (customer fills). Staff click "Send KYC Link" on the customer page, which emails a one-time
   link. The customer verifies with an email OTP, fills the steps, saves drafts, uploads documents, gives
   consent and submits. Keep the existing security: hashed token, expiry, OTP rate limits, PAN and account
   number encrypted and shown masked, no Aadhaar numbers.

C. Manual filling (staff fills). On the customer page add "Fill KYC manually" for staff. It opens the same form
   (same fields, validation and document uploads) inside the staff app without an OTP, creates the
   `KycRequest` with `source=staff` and `filled_by=<user>`, and lets staff upload a scanned signed paper form.
   Staff can also fill the form while on the phone with the customer and then send the link only for
   confirmation (`link_purpose` confirm). Log every save and submit in `KycRequestAuditLog`.

D. Store the PDF. On every submit (online or manual) and again on approval, render the form with
   `link_pdf.render(req)` and save it as a `KycRequestDocument` with a new type `generated_form`, in the same
   private storage as other uploads, named `Customer-Registration-<customer code>-<yyyymmdd-hhmm>.pdf`.
   Keep older versions; mark the latest. Show "Download registration form (PDF)" on the request page and the
   customer page from the stored file (fall back to rendering live only if none is stored). The customer can
   download it after the OTP, sign and stamp it, and upload it back as `signed_form`.

E. Review. Accounts/top management approve or reject (with fields to reopen). On approval, copy the details
   into `Customer` and `CustomerContact` (no duplicate contacts), set payment terms and credit limit, regenerate
   and store the PDF with the approval filled in the office-use panel.

F. Printing rules. Masked PAN (XXXXXX1234F style, last 4 only) and account number (last 4 only). Values the
   customer did not give print as empty boxes so they can be completed by hand. Use Latin-1-safe text with the
   built-in fonts. Normally 2 A4 pages; long content may flow to a 3rd page with the header repeated.

## 4. Tests (Django TestCase, `kyc/tests/`)
- Online flow: link, OTP, draft, upload, submit, PDF stored as `generated_form`, text contains company, GSTIN,
  contacts, masked PAN, and not the full PAN or account number.
- Manual flow: staff fill, `source=staff`, audit log entry, PDF stored, signed scan upload.
- "Same as" copying, new fields validation, new migration applies.
- Approval regenerates the PDF with approval date and approver; customer record updated.
- Permissions: only signed-in staff can fill manually or download stored PDFs; customers only via their link.
Run `python manage.py test kyc` and make sure everything passes.

## 5. Constraints
- Do not commit secrets (`.env*`, `*settings.env`, `cookies.txt`). The git root is `D:\`; stage only files
  inside `Crawler-main` that belong to this feature.
- Keep the existing code style (small service functions in `link_requests.py`, views thin).
- When done, list the changed files, new migration and how to try it locally.
