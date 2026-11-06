import os
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_ALIGN_VERTICAL
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsdecls, qn

def set_cell_background(cell, hex_color):
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = parse_xml(f'<w:shd {nsdecls("w")} w:fill="{hex_color}"/>')
    tc_pr.append(shd)

def set_cell_margins(cell, top=120, bottom=120, left=160, right=160):
    tc_pr = cell._tc.get_or_add_tcPr()
    tcMar = parse_xml(
        f'<w:tcMar {nsdecls("w")}>'
        f'<w:top w:w="{top}" w:type="dxa"/>'
        f'<w:bottom w:w="{bottom}" w:type="dxa"/>'
        f'<w:left w:w="{left}" w:type="dxa"/>'
        f'<w:right w:w="{right}" w:type="dxa"/>'
        f'</w:tcMar>'
    )
    tc_pr.append(tcMar)

def set_table_borders(table, color="D1D5DB", sz="4"):
    tblPr = table._tbl.tblPr
    borders = parse_xml(
        f'<w:tblBorders {nsdecls("w")}>'
        f'<w:top w:val="single" w:sz="{sz}" w:space="0" w:color="{color}"/>'
        f'<w:bottom w:val="single" w:sz="{sz}" w:space="0" w:color="{color}"/>'
        f'<w:insideH w:val="single" w:sz="{sz}" w:space="0" w:color="{color}"/>'
        f'<w:insideV w:val="single" w:sz="{sz}" w:space="0" w:color="{color}"/>'
        f'<w:left w:val="none"/>'
        f'<w:right w:val="none"/>'
        f'</w:tblBorders>'
    )
    tblPr.append(borders)

def make_callout(doc, title, text, border_color="2563EB", bg_color="EFF6FF"):
    tbl = doc.add_table(rows=1, cols=1)
    tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    cell = tbl.cell(0, 0)
    set_cell_background(cell, bg_color)
    set_cell_margins(cell, top=140, bottom=140, left=200, right=200)
    
    tc_pr = cell._tc.get_or_add_tcPr()
    borders = parse_xml(
        f'<w:tcBorders {nsdecls("w")}>'
        f'<w:left w:val="single" w:sz="24" w:space="0" w:color="{border_color}"/>'
        f'<w:top w:val="none"/>'
        f'<w:bottom w:val="none"/>'
        f'<w:right w:val="none"/>'
        f'</w:tcBorders>'
    )
    tc_pr.append(borders)
    
    p = cell.paragraphs[0]
    p.paragraph_format.space_before = Pt(2)
    p.paragraph_format.space_after = Pt(2)
    p.paragraph_format.line_spacing = 1.15
    run_title = p.add_run(f"{title}\n")
    run_title.bold = True
    run_title.font.name = "Arial"
    run_title.font.size = Pt(10)
    run_title.font.color.rgb = RGBColor(0x1D, 0x4E, 0xD8)
    
    run_text = p.add_run(text)
    run_text.font.name = "Arial"
    run_text.font.size = Pt(9.5)
    run_text.font.color.rgb = RGBColor(0x1E, 0x29, 0x3B)
    
    doc.add_paragraph().paragraph_format.space_after = Pt(6)

def add_heading_1(doc, text):
    h = doc.add_paragraph()
    h.paragraph_format.space_before = Pt(18)
    h.paragraph_format.space_after = Pt(6)
    h.paragraph_format.keep_with_next = True
    r = h.add_run(text)
    r.font.name = "Arial"
    r.font.size = Pt(15)
    r.font.bold = True
    r.font.color.rgb = RGBColor(0x0F, 0x22, 0x5A) # Deep Navy
    return h

def add_heading_2(doc, text):
    h = doc.add_paragraph()
    h.paragraph_format.space_before = Pt(12)
    h.paragraph_format.space_after = Pt(4)
    h.paragraph_format.keep_with_next = True
    r = h.add_run(text)
    r.font.name = "Arial"
    r.font.size = Pt(12)
    r.font.bold = True
    r.font.color.rgb = RGBColor(0x25, 0x63, 0xEB) # Royal Blue
    return h

def add_heading_3(doc, text):
    h = doc.add_paragraph()
    h.paragraph_format.space_before = Pt(8)
    h.paragraph_format.space_after = Pt(3)
    h.paragraph_format.keep_with_next = True
    r = h.add_run(text)
    r.font.name = "Arial"
    r.font.size = Pt(10.5)
    r.font.bold = True
    r.font.color.rgb = RGBColor(0x1E, 0x29, 0x3B)
    return h

def add_para(doc, text, bold_prefix=None, space_after=5):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after = Pt(space_after)
    p.paragraph_format.line_spacing = 1.15
    if bold_prefix:
        r_pre = p.add_run(bold_prefix)
        r_pre.font.name = "Arial"
        r_pre.font.size = Pt(10)
        r_pre.font.bold = True
        r_pre.font.color.rgb = RGBColor(0x0F, 0x17, 0x2A)
    r = p.add_run(text)
    r.font.name = "Arial"
    r.font.size = Pt(10)
    r.font.color.rgb = RGBColor(0x33, 0x41, 0x55)
    return p

def add_bullet(doc, text, bold_prefix=None):
    p = doc.add_paragraph(style='List Bullet')
    p.paragraph_format.space_before = Pt(1)
    p.paragraph_format.space_after = Pt(3)
    p.paragraph_format.line_spacing = 1.15
    if bold_prefix:
        r_pre = p.add_run(bold_prefix)
        r_pre.font.name = "Arial"
        r_pre.font.size = Pt(9.5)
        r_pre.font.bold = True
        r_pre.font.color.rgb = RGBColor(0x0F, 0x17, 0x2A)
    r = p.add_run(text)
    r.font.name = "Arial"
    r.font.size = Pt(9.5)
    r.font.color.rgb = RGBColor(0x33, 0x41, 0x55)
    return p

def style_table(table, col_widths, headers, data):
    set_table_borders(table, "CBD5E1", "4")
    table.alignment = WD_TABLE_ALIGNMENT.CENTER

    # Header row
    hdr_cells = table.rows[0].cells
    for i, title in enumerate(headers):
        hdr_cells[i].text = title
        set_cell_background(hdr_cells[i], "0F225A")
        set_cell_margins(hdr_cells[i], top=140, bottom=140, left=140, right=140)
        p = hdr_cells[i].paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.LEFT
        for run in p.runs:
            run.font.name = "Arial"
            run.font.size = Pt(9)
            run.font.bold = True
            run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)

    # Data rows
    for r_idx, row_data in enumerate(data):
        row_cells = table.add_row().cells
        bg_col = "F8FAFC" if (r_idx % 2 == 1) else "FFFFFF"
        for c_idx, val in enumerate(row_data):
            row_cells[c_idx].text = str(val)
            set_cell_background(row_cells[c_idx], bg_col)
            set_cell_margins(row_cells[c_idx], top=100, bottom=100, left=140, right=140)
            p = row_cells[c_idx].paragraphs[0]
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            for run in p.runs:
                run.font.name = "Arial"
                run.font.size = Pt(8.5)
                run.font.color.rgb = RGBColor(0x1E, 0x29, 0x3B)
                if c_idx == 0:
                    run.font.bold = True

    # Set column widths
    for row in table.rows:
        for idx, width in enumerate(col_widths):
            row.cells[idx].width = Inches(width)

def build_document():
    doc = Document()

    # Page Margins
    for section in doc.sections:
        section.top_margin = Inches(0.8)
        section.bottom_margin = Inches(0.8)
        section.left_margin = Inches(0.8)
        section.right_margin = Inches(0.8)

    # ================= COVER / TITLE BLOCK =================
    title_p = doc.add_paragraph()
    title_p.paragraph_format.space_before = Pt(10)
    title_p.paragraph_format.space_after = Pt(4)
    r_badge = title_p.add_run("ENTERPRISE COMMERCIAL PROPOSAL & PRODUCT CATALOGUE\n")
    r_badge.font.name = "Arial"
    r_badge.font.size = Pt(9)
    r_badge.font.bold = True
    r_badge.font.color.rgb = RGBColor(0x25, 0x63, 0xEB)

    r_title = title_p.add_run("SentinelOS™ — Autonomous Telecom AI Governance & Operations Platform")
    r_title.font.name = "Arial"
    r_title.font.size = Pt(22)
    r_title.font.bold = True
    r_title.font.color.rgb = RGBColor(0x0F, 0x22, 0x5A)

    sub_p = doc.add_paragraph()
    sub_p.paragraph_format.space_before = Pt(4)
    sub_p.paragraph_format.space_after = Pt(14)
    r_sub = sub_p.add_run("Complete Module Specifications, Technical Capabilities, and Modular vs. Overall Enterprise Pricing")
    r_sub.font.name = "Arial"
    r_sub.font.size = Pt(11)
    r_sub.font.color.rgb = RGBColor(0x64, 0x74, 0x8B)

    # Metadata Grid Table
    meta_tbl = doc.add_table(rows=2, cols=2)
    meta_tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    set_table_borders(meta_tbl, "CBD5E1", "4")
    
    meta_items = [
        ("TARGET CLIENT AUDIENCE", "Tier-1/Tier-2 Telcos, ISPs & High-Speed FTTH Operators"),
        ("COMMERCIAL CURRENCY", "Indian Rupee (INR ₹) & US Dollar ($ USD Equivalent)"),
        ("RELEASE VERSION", "SentinelOS Enterprise Suite v2.5 (Carrier-Grade)"),
        ("OVERALL PRICING BASELINE", "Full Suite (All 15 Modules): ₹2.50 Lakhs Setup | ₹50,000 / Month")
    ]
    
    idx = 0
    for r in range(2):
        for c in range(2):
            cell = meta_tbl.cell(r, c)
            set_cell_background(cell, "F8FAFC")
            set_cell_margins(cell, top=80, bottom=80, left=120, right=120)
            lbl, val = meta_items[idx]
            cell.text = ""
            p = cell.paragraphs[0]
            p.paragraph_format.space_before = Pt(0)
            p.paragraph_format.space_after = Pt(0)
            p.paragraph_format.line_spacing = 1.15
            r1 = p.add_run(f"{lbl}\n")
            r1.font.name = "Arial"
            r1.font.size = Pt(7.5)
            r1.font.bold = True
            r1.font.color.rgb = RGBColor(0x64, 0x74, 0x8B)
            r2 = p.add_run(val)
            r2.font.name = "Arial"
            r2.font.size = Pt(9.5)
            r2.font.bold = True
            r2.font.color.rgb = RGBColor(0x0F, 0x17, 0x2A)
            idx += 1

    doc.add_paragraph().paragraph_format.space_after = Pt(10)

    # Executive Summary Callout
    make_callout(
        doc,
        "EXECUTIVE COMMERCIAL PROPOSAL SUMMARY",
        "SentinelOS bridges the operational gap between physical optical network degradation, subscriber churn, and billing leakage. "
        "It provides a carrier-grade closed-loop operating loop: Observe → Predict → Recommend → Approve → Execute → Learn.\n\n"
        "• Flagship Full Platform Commercials (Overall 15 Modules): ₹2,50,000 (₹2.50 Lakhs) One-Time Setup & ₹50,000 / month recurring SaaS/AMC.\n"
        "• Modular A-la-Carte Commercials: Individual modules are priced proportionately at ₹12,000–₹25,000 Setup & ₹2,500–₹5,000 / month.\n"
        "• Annual Upfront Incentive: ₹5,00,000 / year (Includes 2 Months Free Subscription, saving ₹1,00,000 annually)."
    )

    # ================= 1. PLATFORM ARCHITECTURE & MODULE SUMMARY =================
    add_heading_1(doc, "1. Executive Platform Architecture & Module Inventory")
    add_para(
        doc,
        "Modern telecom operators face four compounding challenges: siloed telemetry NMS consoles, delayed reactive ticket triage, "
        "high SLA breach penalties on Corporate Internet Leased Lines (ILL), and undetected billing ledger leakages. SentinelOS integrates "
        "the entire operational stack across 15 production-ready functional modules divided into four dedicated strategic pillars:"
    )

    modules_summary_data = [
        ["1. Scored AI Intelligence & Network Assurance", "5 Modules", "Predictive Service Assurance, Churn Prediction, Revenue Assurance, OSS/BSS Orchestration, Customer Journeys"],
        ["2. Governed Field & NOC Operations", "3 Modules", "Autonomous Zero-Touch Dispatch, Carrier-Grade Live NOC GIS Map, Field Operations Management"],
        ["3. Dedicated Portals & Customer Intelligence", "3 Modules", "Field Engineer Mobile Portal, Customer Self-Service Portal, Customer 360 Unified Search"],
        ["4. Enterprise Governance & Platform Core", "4 Modules", "Risk Topology & E2E Connected Trace, Executive Cockpit, Dual-Domain Audit Ledger, Hierarchical RBAC & Multi-Market"]
    ]
    sum_tbl = doc.add_table(rows=1, cols=3)
    style_table(sum_tbl, [2.5, 1.0, 3.4], ["Operational Pillar", "Modules", "Included Systems & Functional Scope"], modules_summary_data)

    doc.add_paragraph().paragraph_format.space_after = Pt(10)

    # ================= 2. DETAILED BREAKDOWN OF ALL 15 MODULES =================
    add_heading_1(doc, "2. Detailed 15-Module Specifications & Capabilities")

    # PILLAR 1
    add_heading_2(doc, "Pillar I: Scored AI Intelligence & Network Assurance (5 Modules)")

    add_heading_3(doc, "Module 1: Predictive Service Assurance & Optical Node Telemetry (/dashboard/assurance)")
    add_para(doc, "Monitors real-time physical optical power, backhaul bandwidth utilization, and packet drops across regional OLT aggregation hubs.")
    add_bullet(doc, "Continuously ingests optical Rx/Tx power (dBm) and flags attenuation drift when signal exceeds the safe -26.5 dBm threshold.", "• Optical Degradation Alerts: ")
    add_bullet(doc, "Automatically calculates the exact count of impacted retail FTTH subscribers and mission-critical Corporate ILL (Internet Leased Line) accounts.", "• Downstream Impact Quantification: ")
    add_bullet(doc, "Breaks down weighted contributing signals (packet loss, CRC errors, fiber micro-bending) so NOC controllers understand why a node is degraded.", "• Explainable AI (XAI) Inspector: ")
    add_bullet(doc, "Setup: ₹22,000 ($270) | Monthly SaaS/AMC: ₹4,500/mo ($55/mo)", "• Standalone Commercials: ")

    add_heading_3(doc, "Module 2: Churn Prediction & Retention AI Engine (/dashboard/churn)")
    add_para(doc, "Applies multi-signal machine learning models to assess subscriber cancellation probability up to 30 days before churn occurs.")
    add_bullet(doc, "Synthesizes repeat technical complaints, optical attenuation history, recurring billing disputes, and speed test drops into a 0-100% score.", "• Multi-Vector Risk Scoring: ")
    add_bullet(doc, "Quantifies total monthly recurring revenue at risk across active rings and ranks high-ARPU enterprise accounts.", "• Revenue Exposure Assessment: ")
    add_bullet(doc, "Generates contextual proactive retention proposals (e.g., speed boost credits, proactive field crew visit, VIP priority routing).", "• Automated Save-Offers: ")
    add_bullet(doc, "Setup: ₹18,000 ($220) | Monthly SaaS/AMC: ₹4,000/mo ($50/mo)", "• Standalone Commercials: ")

    add_heading_3(doc, "Module 3: Revenue Assurance & Billing Leakage Analytics (/dashboard/revenue)")
    add_para(doc, "Performs automated reconciliation across RADIUS accounting sessions, network provisioning, and billing ledgers (SAP BRIM / custom BSS).")
    add_bullet(doc, "Identifies active turbo speed boosts and commercial add-ons configured on BRAS/BNG without recurring billing line items.", "• Unbilled Speed Add-On Detection: ")
    add_bullet(doc, "Detects plan speed mismatches, uncollected dunning balances, and accidental duplicate SLA credit waivers.", "• Catalog Rate Reconciliation: ")
    add_bullet(doc, "Generates cryptographic ledger adjustment receipts with full before-and-after transaction logs.", "• Audit-Compliant Recovery: ")
    add_bullet(doc, "Setup: ₹20,000 ($240) | Monthly SaaS/AMC: ₹4,000/mo ($50/mo)", "• Standalone Commercials: ")

    add_heading_3(doc, "Module 4: OSS/BSS Orchestration & Auto-Remediation Queue (/dashboard/orchestration)")
    add_para(doc, "Executes closed-loop remediation workflows to resolve routine subscriber faults without manual NOC intervention.")
    add_bullet(doc, "Automatically dispatches TR-069 reboot commands, optical profile resets, and BRAS QoS bandwidth re-syncs.", "• Zero-Touch Device Management: ")
    add_bullet(doc, "Classifies incidents dynamically into automated software remediation versus physical field splicing dispatch.", "• Dynamic Decision Branching: ")
    add_bullet(doc, "Reduces Mean Time to Resolution (MTTR) from hours to 4.2 minutes for common configuration and authentication glitches.", "• SLA Optimization: ")
    add_bullet(doc, "Setup: ₹18,000 ($220) | Monthly SaaS/AMC: ₹3,500/mo ($45/mo)", "• Standalone Commercials: ")

    add_heading_3(doc, "Module 5: Customer Journeys & Next-Best-Action (NBA) Engine (/dashboard/journeys)")
    add_para(doc, "Automates end-to-end subscriber lifecycle engagement across 6 strategic operational stages.")
    add_bullet(doc, "Covers Acquisition, Installation, Usage, Renewal, Complaint Resolution, and Win-back lifecycles.", "• 6 Lifecycle Stages: ")
    add_bullet(doc, "Determines the optimal communication channel (WhatsApp Business API, SMS, automated outbound call, or VIP Care call).", "• Omnichannel Routing: ")
    add_bullet(doc, "Prevents customer fatigue by enforcing frequency capping and role-approved engagement policies.", "• Governance-Aware Engagement: ")
    add_bullet(doc, "Setup: ₹15,000 ($180) | Monthly SaaS/AMC: ₹3,000/mo ($38/mo)", "• Standalone Commercials: ")

    # PILLAR 2
    add_heading_2(doc, "Pillar II: Governed Field & NOC Operations (3 Modules)")

    add_heading_3(doc, "Module 6: Automatic Ticketing & Autonomous Closed-Loop Dispatch (/dashboard/ticketing)")
    add_para(doc, "The operational core of zero-touch field management, eliminating traditional dispatcher delays.")
    add_bullet(doc, "Enables zero-touch autonomous assignment for routine P3/P4 tickets in less than 1 second.", "• Autonomous Dispatch Toggle: ")
    add_bullet(doc, "Evaluates live GPS proximity, technician certification (optical fusion splicing vs CPE Wi-Fi setup), and active queue capacity.", "• Multi-Factor Dispatch Algorithm: ")
    add_bullet(doc, "Enforces mandatory human sign-off for critical P1/P2 fiber outages, triggering automated emergency IVR phone alerts to NOC supervisors.", "• Automated Emergency IVR Alert: ")
    add_bullet(doc, "Visualizes regional technician capacity and prevents burnout through intelligent job allocation.", "• Resource Timeline Balancing: ")
    add_bullet(doc, "Setup: ₹22,000 ($270) | Monthly SaaS/AMC: ₹4,500/mo ($55/mo)", "• Standalone Commercials: ")

    add_heading_3(doc, "Module 7: Carrier-Grade NOC Live GIS Tracking Map (/dashboard/noc-tracking)")
    add_para(doc, "A 24/7 dark-mode telecom command center providing synchronized real-time visibility into workforce mobility and physical optical assets.")
    add_bullet(doc, "Displays pulsing live green heartbeats (<5 min freshness), dynamically shifting to Stale (5-15 min) or Offline (>15 min) status.", "• Real-Time GPS Tracking: ")
    add_bullet(doc, "Offers 5 comprehensive diagnostic tabs: Device Health, Historical Telemetry Charts (1h/6h/24h), Downstream Impact Analysis, and Test Scenario Simulation.", "• 5-Tab Hardware Multi-Inspector: ")
    add_bullet(doc, "Visualizes exact optical breadcrumbs: Customer ONT → Local Splitter → Fiber Distribution Cabinet → PON Port → Main OLT Chassis.", "• Dynamic Reverse Optical Path: ")
    add_bullet(doc, "Seamless toggle between open-source MapLibre vector tiles (eliminating Google Maps API costs) and standard Google Maps.", "• Dual-Engine GIS Framework: ")
    add_bullet(doc, "Setup: ₹25,000 ($300) | Monthly SaaS/AMC: ₹5,000/mo ($60/mo)", "• Standalone Commercials: ")

    add_heading_3(doc, "Module 8: Field Operations & Workforce Management (/dashboard/field-operations)")
    add_para(doc, "Centralized field management console for shift leads, area supervisors, and fleet managers.")
    add_bullet(doc, "Real-time tracking of technician states: Dispatched, En-Route, On-Site, In-Progress, and Completed.", "• Lifecycle Job Tracking: ")
    add_bullet(doc, "Renders optimal routing polylines, calculates estimated arrival times (ETA), and flags traffic congestion delays.", "• Turn-by-Turn Navigation & ETA: ")
    add_bullet(doc, "Monitors regional SLA commitment timers, alerting dispatchers before penalties are triggered.", "• SLA Breach Early Warning: ")
    add_bullet(doc, "Setup: ₹18,000 ($220) | Monthly SaaS/AMC: ₹3,500/mo ($45/mo)", "• Standalone Commercials: ")

    # PILLAR 3
    add_heading_2(doc, "Pillar III: Dedicated Portals & Customer Intelligence (3 Modules)")

    add_heading_3(doc, "Module 9: Field Engineer Mobile Portal & Telemetry (/portal/engineer)")
    add_para(doc, "A dedicated mobile-responsive Progressive Web App (PWA) tailored for on-ground field technicians.")
    add_bullet(doc, "Displays assigned work orders, customer addresses, priority tags, and turn-by-turn navigation links.", "• Mobile Task Workbench: ")
    add_bullet(doc, "Transmits background GPS breadcrumbs and device battery status back to the NOC console.", "• Live Location Telemetry: ")
    add_bullet(doc, "Prevents fraudulent job closures through Redis-backed, time-limited one-time password verification from the customer.", "• Cryptographic OTP Verification: ")
    add_bullet(doc, "Queues status transitions and optical test readings when working in basements, syncing once connectivity is restored.", "• Offline Capability: ")
    add_bullet(doc, "Setup: ₹18,000 ($220) | Monthly SaaS/AMC: ₹3,500/mo ($45/mo)", "• Standalone Commercials: ")

    add_heading_3(doc, "Module 10: Customer Self-Service Portal (/portal/customer)")
    add_para(doc, "Customer-facing interface that eliminates inbound call center load and builds transparent subscriber trust.")
    add_bullet(doc, "Subscribers can log service degradation or connectivity loss tickets in two clicks.", "• Self-Service Ticket Logging: ")
    add_bullet(doc, "Displays real-time on-map location of the en-route technician and arrival ETA, replicating premium delivery experiences.", "• Live Technician Uber-Like Tracking: ")
    add_bullet(doc, "Displays active broadband plan speeds, data usage consumption, and recent invoice receipts.", "• Account & Plan Visibility: ")
    add_bullet(doc, "Setup: ₹15,000 ($180) | Monthly SaaS/AMC: ₹3,000/mo ($38/mo)", "• Standalone Commercials: ")

    add_heading_3(doc, "Module 11: Customer 360 Unified Intelligence (/dashboard/customer360)")
    add_para(doc, "A unified 360-degree subscriber intelligence console for Tier-2/Tier-3 support and NOC escalation teams.")
    add_bullet(doc, "Instant lookup by Customer ID, mobile number (MSISDN), ONT serial number, or physical street address.", "• Fast Global Search: ")
    add_bullet(doc, "Displays linked OLT chassis, PON port, active tickets, historical invoices, and live churn risk score in a single pane.", "• Integrated Data Aggregation: ")
    add_bullet(doc, "Enables support agents to trigger one-click line diagnostic tests and view optical attenuation logs.", "• Direct Action Capabilities: ")
    add_bullet(doc, "Setup: ₹12,000 ($150) | Monthly SaaS/AMC: ₹2,500/mo ($30/mo)", "• Standalone Commercials: ")

    # PILLAR 4
    add_heading_2(doc, "Pillar IV: Enterprise Governance, Security & Management (4 Modules)")

    add_heading_3(doc, "Module 12: Risk Topology & Connected E2E Trace (/dashboard/pilot-bundle)")
    add_para(doc, "The flagship executive capability demonstrating the complete connected operational loop.")
    add_bullet(doc, "Walks through the complete closed loop: Step 1 (Observe) → Step 2 (Predict) → Step 3 (Recommend) → Step 4 (Approve) → Step 5 (Execute) → Step 6 (Learn).", "• 6-Phase Operating Loop: ")
    add_bullet(doc, "Visually connects a physical optical dBm drop at the OLT directly to enterprise customer churn and ARR impact.", "• End-to-End Cause & Effect: ")
    add_bullet(doc, "Ideal for executive presentations, boardroom reviews, and fast proof-of-concept deployments.", "• Flagship Pilot Core: ")
    add_bullet(doc, "Setup: ₹22,000 ($270) | Monthly SaaS/AMC: ₹4,500/mo ($55/mo)", "• Standalone Commercials: ")

    add_heading_3(doc, "Module 13: Executive Cockpit & Analytics Overview (/dashboard/cockpit)")
    add_para(doc, "High-level command center designed for CTOs, VPs of Operations, and Heads of Network.")
    add_bullet(doc, "Summarizes overall network availability, total active alarms, and critical fiber cuts across all regions.", "• Fleet Health Scorecard: ")
    add_bullet(doc, "Aggregates enterprise contracts currently exposed to service SLA penalties and subscriber cancellation.", "• At-Risk ARR Exposure: ")
    add_bullet(doc, "Tracks governance approval turnaround time, ensuring managerial sign-offs happen in under 5 minutes.", "• Operational SLA Compliance: ")
    add_bullet(doc, "Setup: ₹12,000 ($150) | Monthly SaaS/AMC: ₹2,500/mo ($30/mo)", "• Standalone Commercials: ")

    add_heading_3(doc, "Module 14: Dual-Domain AI Governance & Cryptographic Audit Ledger (/dashboard/governance)")
    add_para(doc, "Eliminates black-box autonomous AI risks by enforcing strict human-in-the-loop validation.")
    add_bullet(doc, "Critical actions require dual independent approval (NOC lead signs off on physical dispatch; Customer Care signs off on customer credits).", "• Dual-Domain Sign-Off: ")
    add_bullet(doc, "Every approval, rejection, and system dispatch is recorded with evaluating user, timestamp, telemetry inputs, and cryptographic hash.", "• Immutable Audit Trail: ")
    add_bullet(doc, "Provides full compliance documentation for internal audits and regulatory scrutiny.", "• Enterprise Compliance: ")
    add_bullet(doc, "Setup: ₹15,000 ($180) | Monthly SaaS/AMC: ₹3,000/mo ($38/mo)", "• Standalone Commercials: ")

    add_heading_3(doc, "Module 15: Enterprise RBAC, Security & Multi-Market Telemetry (/dashboard/roles)")
    add_para(doc, "Core identity, access control, and multi-tenant security layer.")
    add_bullet(doc, "Enforces strict role boundaries (Super Admin, NOC Controller, Care Agent, Revenue Auditor, Executive, Field Engineer, Customer, Viewer).", "• Hierarchical Role Matrix: ")
    add_bullet(doc, "PBKDF2 salted password hashing (100,000 iterations), signed JWT tokens, and strict rate-limiting.", "• Carrier-Grade Security: ")
    add_bullet(doc, "Isolates network telemetry, customer data, and technician rosters across independent regional markets (e.g., Mumbai, Kolkata, Delhi).", "• Multi-Market Partitioning: ")
    add_bullet(doc, "Setup: ₹13,000 ($160) | Monthly SaaS/AMC: ₹2,500/mo ($30/mo)", "• Standalone Commercials: ")

    doc.add_paragraph().paragraph_format.space_after = Pt(12)

    # ================= 3. COMPREHENSIVE MODULAR (A-LA-CARTE) PRICING TABLE =================
    add_heading_1(doc, "3. Standalone Modular (A-La-Carte) Pricing Catalogue")
    add_para(
        doc,
        "Clients have full flexibility to procure modules individually to complement their existing NMS, CRM, or ticketing systems. "
        "The table below details the proportionate standalone commercials based on the overall ₹2.5 Lakhs setup and ₹50,000/month baseline:"
    )

    pricing_table_data = [
        ["1. Predictive Service Assurance", "Network NOC", "₹22,000", "₹4,500 / mo", "$270 / $55 mo"],
        ["2. Churn Prediction & Retention AI", "Customer Care", "₹18,000", "₹4,000 / mo", "$220 / $50 mo"],
        ["3. Revenue Assurance & Leakage", "Revenue & Billing", "₹20,000", "₹4,000 / mo", "$240 / $50 mo"],
        ["4. OSS/BSS Orchestration Queue", "Network Operations", "₹18,000", "₹3,500 / mo", "$220 / $45 mo"],
        ["5. Customer Journeys & NBA", "Growth & Retention", "₹15,000", "₹3,000 / mo", "$180 / $38 mo"],
        ["6. Auto-Ticketing & Dispatch", "NOC & Dispatch", "₹22,000", "₹4,500 / mo", "$270 / $55 mo"],
        ["7. NOC Live GIS Tracking Map", "24/7 NOC Center", "₹25,000", "₹5,000 / mo", "$300 / $60 mo"],
        ["8. Field Operations & Fleet", "Workforce Fleet", "₹18,000", "₹3,500 / mo", "$220 / $45 mo"],
        ["9. Field Engineer Mobile Portal", "Field Engineers", "₹18,000", "₹3,500 / mo", "$220 / $45 mo"],
        ["10. Customer Self-Service Portal", "Subscribers", "₹15,000", "₹3,000 / mo", "$180 / $38 mo"],
        ["11. Customer 360 Intelligence", "Support & Care", "₹12,000", "₹2,500 / mo", "$150 / $30 mo"],
        ["12. Risk Topology & E2E Trace", "Executive & NOC", "₹22,000", "₹4,500 / mo", "$270 / $55 mo"],
        ["13. Executive Cockpit Analytics", "C-Suite & Heads", "₹12,000", "₹2,500 / mo", "$150 / $30 mo"],
        ["14. Dual Governance & Audit", "Compliance / Security", "₹15,000", "₹3,000 / mo", "$180 / $38 mo"],
        ["15. Enterprise RBAC & Security", "Administration", "₹13,000", "₹2,500 / mo", "$160 / $30 mo"],
        ["TOTAL A-LA-CARTE (SUM)", "ALL 15 MODULES", "₹2,65,000", "₹53,500 / mo", "$3,200 / $640 mo"],
        ["OVERALL BUNDLE (ALL 15)", "FLAGSHIP CARRIER DEAL", "₹2,50,000", "₹50,000 / mo", "$3,000 / $600 mo"]
    ]

    p_tbl = doc.add_table(rows=1, cols=5)
    style_table(p_tbl, [2.2, 1.3, 1.1, 1.2, 1.2], ["Module Name", "Operational Domain", "Setup Fee (INR)", "Monthly Recurring", "USD Equivalent"], pricing_table_data)

    doc.add_paragraph().paragraph_format.space_after = Pt(12)

    # ================= 4. OVERALL BUNDLED PACKAGES =================
    add_heading_1(doc, "4. Overall Bundled Commercial Packages (Enterprise Suites)")
    add_para(
        doc,
        "SentinelOS offers attractive, pre-packaged tiers tailored to operational needs. The premier package is the "
        "SentinelOS Enterprise Carrier Edition providing the entire 15-module ecosystem at a flat ₹2.50 Lakhs setup and ₹50,000/month:"
    )

    bundle_overview_data = [
        ["Tier 1: Pilot / Foundation Bundle", "4 Core Modules", "₹75,000", "₹18,000 / mo", "$900 / $220 mo", "Pilot PoC"],
        ["Tier 2: NOC & Field Operations Suite", "7 Operational Modules", "₹1,45,000", "₹32,000 / mo", "$1,750 / $390 mo", "Operations"],
        ["Tier 3: Customer Care & Retention Suite", "6 Business Modules", "₹1,15,000", "₹26,000 / mo", "$1,400 / $315 mo", "Retention"],
        ["Tier 4: Enterprise Carrier Edition (OVERALL)", "ALL 15 MODULES (FULL)", "₹2,50,000", "₹50,000 / mo", "$3,000 / $600 mo", "Full Suite"]
    ]

    b_tbl = doc.add_table(rows=1, cols=6)
    style_table(b_tbl, [1.8, 1.1, 1.0, 1.0, 1.2, 0.9], ["Package Tier", "Scope", "Setup (INR)", "Monthly (INR)", "USD Equivalent", "Strategic Tier"], bundle_overview_data)

    doc.add_paragraph().paragraph_format.space_after = Pt(10)

    # Detailed Package Descriptions
    add_heading_2(doc, "Detailed Package Scope & Specifications")

    # Package 1
    add_heading_3(doc, "Tier 1: Pilot / Foundation Proof-of-Concept Bundle")
    add_para(doc, "Target Audience: Operators seeking a fast, low-risk 2-to-4 week validation on a targeted OLT ring or pilot cluster.")
    add_bullet(doc, "Risk Topology & E2E Connected Trace, Predictive Service Assurance, Automatic Ticketing & Dispatch, Executive Cockpit.", "• Included Modules (4): ")
    add_bullet(doc, "One-Time Setup & Deployment: ₹75,000 ($900) | Monthly Subscription: ₹18,000 / month ($220 / mo)", "• Commercials: ")
    add_bullet(doc, "Covers up to 10,000 active subscribers on 1 regional market cluster (e.g., 5-10 OLT chassis). Includes standard SNMP telemetry onboarding.", "• Deployment Terms: ")

    # Package 2
    add_heading_3(doc, "Tier 2: NOC & Field Operations Enterprise Suite")
    add_para(doc, "Target Audience: ISPs looking to cut MTTR, automate field dispatch, and eliminate blind truck rolls.")
    add_bullet(doc, "Carrier-Grade NOC Live GIS Tracking Map, Automatic Ticketing & Autonomous Dispatch, Field Operations Management, Field Engineer Mobile Portal, Predictive Service Assurance, OSS/BSS Orchestration, and Enterprise RBAC.", "• Included Modules (7): ")
    add_bullet(doc, "One-Time Setup & Deployment: ₹1,45,000 ($1,750) | Monthly Subscription: ₹32,000 / month ($390 / mo)", "• Commercials: ")
    add_bullet(doc, "Covers up to 50 field technicians, unlimited NOC controllers, and dual-engine GIS vector map tiles with zero external Google Maps fees.", "• Key Highlights: ")

    # Package 3
    add_heading_3(doc, "Tier 3: Customer Care & Revenue Assurance Suite")
    add_para(doc, "Target Audience: Commercial directors and Customer Support heads aiming to stop subscriber churn and recover unbilled service revenue.")
    add_bullet(doc, "Churn Prediction AI, Revenue Assurance & Billing Leakage Analytics, Customer Journeys & NBA Engine, Customer 360 Intelligence, Customer Self-Service Portal, and Executive Cockpit.", "• Included Modules (6): ")
    add_bullet(doc, "One-Time Setup & Deployment: ₹1,15,000 ($1,400) | Monthly Subscription: ₹26,000 / month ($315 / mo)", "• Commercials: ")
    add_bullet(doc, "Direct integration with CRM and BSS/Billing ledgers to surface unbilled turbo add-ons and provide automated subscriber save-offers.", "• Key Highlights: ")

    # Package 4
    add_heading_3(doc, "Tier 4: SentinelOS Enterprise Carrier Edition (Complete Full Suite - Overall)")
    add_para(doc, "Target Audience: Complete end-to-end autonomous operations for regional telecom operators and growing national FTTH broadband providers.")
    add_bullet(doc, "ALL 15 MODULES UNRESTRICTED: Complete AI Engines, Live NOC Map, Field & Customer Portals, Dual Sign-off Governance, Automated IVR Calls, and Multi-Market Partitioning.", "• Included Modules (15): ")
    add_bullet(doc, "One-Time Setup & Implementation: ₹2,50,000 ($3,000) | Monthly Subscription / Enterprise License: ₹50,000 / month ($600 / mo)", "• Flat Commercials: ")
    add_bullet(doc, "₹5,00,000 / year (Pay for 10 months and get 2 Months FREE Subscription + Priority 24x7 SLA Support).", "• Annual Upfront Incentive: ")
    add_bullet(doc, "Multi-market support (e.g., Mumbai, Kolkata, Delhi, Bangalore), dedicated solution architect, and guaranteed 99.9% uptime SLA.", "• Enterprise Deliverables: ")

    doc.add_paragraph().paragraph_format.space_after = Pt(12)

    # ================= 5. RETURN ON INVESTMENT & CLIENT BUSINESS CASE =================
    add_heading_1(doc, "5. Return on Investment (ROI) & Strategic Business Case")
    add_para(
        doc,
        "At an overall investment of just ₹2.50 Lakhs setup and ₹50,000/month, SentinelOS delivers a payback period within 30 to 45 days:"
    )

    make_callout(
        doc,
        "KEY COMMERCIAL METRICS & RETURN ON INVESTMENT",
        "• 65% Reduction in Mean Time to Resolution (MTTR): Direct reduction in field technician travel and labor overhead.\n"
        "• Zero Map Licensing Expenditure: Built-in MapLibre vector engine eliminates Google Maps tile billing, saving ₹1.2L–₹2.0L annually.\n"
        "• 40% Reduction in Corporate ILL SLA Penalties: Proactive optical attenuation detection fixes fiber degradation 4-6 hours before downtime.\n"
        "• ₹1.5L–₹3.5L Monthly Recovered Billing Leakage: Discovers unbilled speed boosts and catalog pricing mismatches in existing billing ledgers.\n"
        "• 80% Zero-Touch Ticket Resolution: Frees Tier-1/Tier-2 support teams from routine manual dispatching tasks.",
        border_color="059669",
        bg_color="ECFDF5"
    )

    # ================= 6. IMPLEMENTATION ROADMAP & TERMS =================
    add_heading_1(doc, "6. Implementation Roadmap, Milestones & Commercial Terms")

    add_heading_2(doc, "Standard 4-Week Production Implementation Timeline")
    roadmap_data = [
        ["Week 1: Environment & Network Ingestion", "Deploy containerized backend and PostgreSQL database; establish secure SNMP trap / telemetry polling from aggregation OLTs and BRAS nodes."],
        ["Week 2: GIS Topology & Roster Provisioning", "Ingest physical GIS fiber routes, cabinet locations, and customer premise coordinates; configure technician roster, skill certifications, and RBAC matrix."],
        ["Week 3: Closed-Loop Dispatch & User Training", "Execute live field simulation tests; calibrate autonomous P3/P4 dispatch confidence thresholds; conduct role-based training for NOC controllers and field engineers."],
        ["Week 4: Full Production Go-Live & Review", "Enable real-time telemetry streaming; transition customer care to Customer 360; initiate monthly SLA tracking and performance benchmarks."]
    ]
    r_tbl = doc.add_table(rows=1, cols=2)
    style_table(r_tbl, [2.5, 4.5], ["Project Milestone", "Key Deliverables & Operational Focus"], roadmap_data)

    doc.add_paragraph().paragraph_format.space_after = Pt(10)

    add_heading_2(doc, "Commercial Terms & Payment Conditions")
    add_bullet(doc, "50% advance upon contract signing (₹1,25,000); 50% upon official production go-live (₹1,25,000).", "• Setup Fee Milestones: ")
    add_bullet(doc, "Billed in advance on a monthly, quarterly, or annual cycle. Net 15 days payment terms.", "• Recurring Subscription: ")
    add_bullet(doc, "Annual advance payment of ₹5,00,000 includes 2 months free subscription (saving ₹1,00,000).", "• Annual Discount: ")
    add_bullet(doc, "Standard commercial pricing excludes applicable Goods & Services Tax (GST 18%) or local international withholding taxes.", "• Tax Exclusions: ")
    add_bullet(doc, "Available on managed Cloud SaaS (AWS/Azure/GCP) or fully isolated On-Premises bare-metal / private OpenStack infrastructure.", "• Deployment Models: ")
    add_bullet(doc, "Includes business-hours 99.5% support for Tier 1-3 packages; 24x7x365 dedicated emergency phone & ticket support for Tier 4 Carrier Edition.", "• SLA Support Guarantee: ")

    doc.add_paragraph().paragraph_format.space_after = Pt(14)

    # Signature Block
    sig_tbl = doc.add_table(rows=1, cols=2)
    sig_tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    set_table_borders(sig_tbl, "CBD5E1", "4")
    
    cell_l = sig_tbl.cell(0, 0)
    set_cell_background(cell_l, "F8FAFC")
    set_cell_margins(cell_l, top=100, bottom=100, left=140, right=140)
    cell_l.text = "PROPOSED BY:\nPMRG Solutions LLP\nAuthorized Signatory:\n___________________________\nTitle: Head of Telecom Solutions\nDate: October 06, 2026"
    for p in cell_l.paragraphs:
        for r in p.runs:
            r.font.name = "Arial"
            r.font.size = Pt(8.5)

    cell_r = sig_tbl.cell(0, 1)
    set_cell_background(cell_r, "F8FAFC")
    set_cell_margins(cell_r, top=100, bottom=100, left=140, right=140)
    cell_r.text = "ACCEPTED & AGREED:\nClient Enterprise Name:\nAuthorized Signatory:\n___________________________\nTitle:\nDate:"
    for p in cell_r.paragraphs:
        for r in p.runs:
            r.font.name = "Arial"
            r.font.size = Pt(8.5)

    # Save documents with fallback in case user currently has the file open in Microsoft Word
    target_dirs = [
        r"c:\Users\Lenovo\Documents\GitHub\jeebr_backend",
        r"c:\Users\Lenovo\Documents\GitHub\jeebr_frontend"
    ]
    file_names = [
        "SentinelOS_Modules_and_Pricing_Proposal_Updated.docx",
        "SentinelOS_Modules_and_Pricing_Proposal.docx"
    ]

    for d in target_dirs:
        for fname in file_names:
            target_path = os.path.join(d, fname)
            try:
                doc.save(target_path)
                print(f"Successfully saved: {target_path}")
            except Exception as e:
                print(f"Could not save {target_path} (likely open in Word): {e}")

if __name__ == "__main__":
    build_document()
