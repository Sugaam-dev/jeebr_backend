import os
import sys
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether, HRFlowable
)
from reportlab.pdfgen import canvas

class NumberedCanvas(canvas.Canvas):
    """
    Two-pass canvas to dynamically compute and print total page count 'Page X of Y'.
    Also adds professional running headers and footers.
    """
    def __init__(self, *args, **kwargs):
        super(NumberedCanvas, self).__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_decorations(num_pages)
            super(NumberedCanvas, self).showPage()
        super(NumberedCanvas, self).save()

    def draw_page_decorations(self, page_count):
        self.saveState()
        self.setFont("Helvetica", 8)
        self.setFillColor(colors.HexColor("#64748B"))
        
        # We don't draw running header on cover/page 1 if we treat it differently,
        # but here we can draw standard header from page 2 onwards
        if self._pageNumber > 1:
            # Running Header
            self.drawString(36, 805, "JEEBR AI | 30-Minute Product Demo Master Script (African Telecom / ISP Focus)")
            self.setStrokeColor(colors.HexColor("#CBD5E1"))
            self.setLineWidth(0.5)
            self.line(36, 800, 559, 800)

        # Running Footer (on all pages)
        self.setStrokeColor(colors.HexColor("#E2E8F0"))
        self.setLineWidth(0.5)
        self.line(36, 42, 559, 42)
        
        self.drawString(36, 30, "CONFIDENTIAL & PROPRIETARY — FOR INTERNAL DEMO DELIVERY")
        page_str = f"Page {self._pageNumber} of {page_count}"
        self.drawRightString(559, 30, page_str)
        self.restoreState()


def build_pdf(filename):
    doc = SimpleDocTemplate(
        filename,
        pagesize=A4,
        leftMargin=36,
        rightMargin=36,
        topMargin=46,
        bottomMargin=48
    )

    styles = getSampleStyleSheet()
    
    # Custom styles
    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=22,
        leading=26,
        textColor=colors.HexColor("#0F172A"),
        spaceAfter=4
    )
    
    subtitle_style = ParagraphStyle(
        'DocSubtitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=12,
        leading=16,
        textColor=colors.HexColor("#2563EB"),
        spaceAfter=12
    )

    meta_style = ParagraphStyle(
        'MetaStyle',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9,
        leading=13,
        textColor=colors.HexColor("#475569")
    )

    meta_bold = ParagraphStyle(
        'MetaBold',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=9,
        leading=13,
        textColor=colors.HexColor("#1E293B")
    )

    section_heading = ParagraphStyle(
        'SectionHeading',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=14,
        leading=18,
        textColor=colors.HexColor("#1E3A8A"),
        spaceBefore=14,
        spaceAfter=6,
        keepWithNext=True
    )

    phase_heading = ParagraphStyle(
        'PhaseHeading',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=11,
        leading=15,
        textColor=colors.HexColor("#0F172A"),
        spaceBefore=8,
        spaceAfter=4,
        keepWithNext=True
    )

    body_style = ParagraphStyle(
        'Body',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9,
        leading=13,
        textColor=colors.HexColor("#1E293B")
    )

    body_bold = ParagraphStyle(
        'BodyBold',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=9,
        leading=13,
        textColor=colors.HexColor("#0F172A")
    )

    script_spoken = ParagraphStyle(
        'ScriptSpoken',
        parent=styles['Normal'],
        fontName='Helvetica-Oblique',
        fontSize=9,
        leading=13.5,
        textColor=colors.HexColor("#0F172A")
    )

    screen_label = ParagraphStyle(
        'ScreenLabel',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8.5,
        leading=12,
        textColor=colors.HexColor("#1D4ED8")
    )

    african_context_style = ParagraphStyle(
        'AfricanContext',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8.5,
        leading=12,
        textColor=colors.HexColor("#92400E")
    )

    table_header = ParagraphStyle(
        'TableHeader',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8.5,
        leading=11,
        textColor=colors.white
    )

    table_cell = ParagraphStyle(
        'TableCell',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8,
        leading=11,
        textColor=colors.HexColor("#1E293B")
    )

    table_cell_bold = ParagraphStyle(
        'TableCellBold',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8,
        leading=11,
        textColor=colors.HexColor("#0F172A")
    )

    faq_q = ParagraphStyle(
        'FaqQ',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=9.5,
        leading=13,
        textColor=colors.HexColor("#1E3A8A"),
        spaceBefore=6,
        spaceAfter=2,
        keepWithNext=True
    )

    faq_a = ParagraphStyle(
        'FaqA',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8.5,
        leading=12.5,
        textColor=colors.HexColor("#1E293B"),
        spaceAfter=6
    )

    story = []

    # ---------------------------------------------------------
    # HEADER / TITLE BLOCK
    # ---------------------------------------------------------
    story.append(Paragraph("JEEBR AI | EXECUTIVE DEMO SCRIPT", title_style))
    story.append(Paragraph("30-Minute Master Sales & Solution Presentation for African Telecoms & ISPs", subtitle_style))
    
    # Meta Info Card Table
    meta_data = [
        [
            Paragraph("<b>Target Audience:</b> CXOs, VP Operations, NOC & Customer Care Directors", meta_style),
            Paragraph("<b>Format & Duration:</b> Live Screen Share / 30 Minutes", meta_style)
        ],
        [
            Paragraph("<b>Target Markets:</b> Kenya, Nigeria, South Africa, Ghana, Regional ISPs", meta_style),
            Paragraph("<b>Key Goals:</b> Reduce MTTR & Truck-Rolls, Prevent Churn, Stop Revenue Leakage", meta_style)
        ]
    ]
    meta_table = Table(meta_data, colWidths=[270, 253])
    meta_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
        ('BOX', (0, 0), (-1, -1), 1, colors.HexColor("#E2E8F0")),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
        ('PADDING', (0, 0), (-1, -1), 6),
    ]))
    story.append(meta_table)
    story.append(Spacer(1, 10))

    # ---------------------------------------------------------
    # 30-MINUTE TIMELINE BREAKDOWN
    # ---------------------------------------------------------
    story.append(Paragraph("1. Agenda & 30-Minute Timing Breakdown", section_heading))
    
    timeline_data = [
        [
            Paragraph("Time", table_header),
            Paragraph("Phase / Module", table_header),
            Paragraph("Screen / URL", table_header),
            Paragraph("Core Message & Business Impact", table_header)
        ],
        [
            Paragraph("00:00 - 03:00", table_cell_bold),
            Paragraph("Opening & Alignment", table_cell),
            Paragraph("Landing / Camera", table_cell),
            Paragraph("Acknowledge African network realities (fiber cuts, truck rolls, power flaps).", table_cell)
        ],
        [
            Paragraph("03:00 - 06:00", table_cell_bold),
            Paragraph("Architecture & Value", table_cell),
            Paragraph("Landing Page", table_cell),
            Paragraph("Vendor-agnostic OSS/BSS AI layer (Huawei, ZTE, Nokia, ACS, Radius).", table_cell)
        ],
        [
            Paragraph("06:00 - 10:00", table_cell_bold),
            Paragraph("Executive Cockpit", table_cell),
            Paragraph("<code>/cockpit</code>", table_cell),
            Paragraph("Single pane of glass: Network health index, churn exposure, unbilled leakage.", table_cell)
        ],
        [
            Paragraph("10:00 - 15:00", table_cell_bold),
            Paragraph("Predictive Assurance (NOC)", table_cell),
            Paragraph("<code>/assurance</code>", table_cell),
            Paragraph("Optical degradation alerts (< -27 dBm) to catch fiber breaks before outages occur.", table_cell)
        ],
        [
            Paragraph("15:00 - 19:00", table_cell_bold),
            Paragraph("Orchestration & Field Force", table_cell),
            Paragraph("<code>/orchestration</code><br/><code>/field-operations</code>", table_cell),
            Paragraph("TR-069 zero-touch remote fixes vs. nearest-technician GPS dispatch.", table_cell)
        ],
        [
            Paragraph("19:00 - 23:00", table_cell_bold),
            Paragraph("Churn & Revenue Assurance", table_cell),
            Paragraph("<code>/churn</code><br/><code>/revenue</code>", table_cell),
            Paragraph("High-ARPU retention offers and instant billing/provisioning mismatch fixes.", table_cell)
        ],
        [
            Paragraph("23:00 - 25:00", table_cell_bold),
            Paragraph("Governance & Audit Trail", table_cell),
            Paragraph("<code>/governance</code>", table_cell),
            Paragraph("Human-in-the-Loop approval queues for enterprise safety and telco compliance.", table_cell)
        ],
        [
            Paragraph("25:00 - 27:00", table_cell_bold),
            Paragraph("Customer 360", table_cell),
            Paragraph("<code>/customer360</code>", table_cell),
            Paragraph("Unified subscriber view and live technician tracking on customer mobile.", table_cell)
        ],
        [
            Paragraph("27:00 - 30:00", table_cell_bold),
            Paragraph("Q&A & 30-Day Pilot", table_cell),
            Paragraph("Camera / Slide", table_cell),
            Paragraph("4-Week risk-free regional cluster pilot proposal.", table_cell)
        ]
    ]

    t_table = Table(timeline_data, colWidths=[65, 120, 110, 228])
    t_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#1E3A8A")),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ('PADDING', (0, 0), (-1, -1), 4.5),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
    ]))
    story.append(t_table)
    story.append(Spacer(1, 10))

    # Helper function to generate clean module boxes
    def add_phase_box(phase_title, screen_info, spoken_text, african_nuance, checkpoint_q=None):
        content = []
        content.append(Paragraph(phase_title, phase_heading))
        
        # Details table
        box_data = [
            [
                Paragraph("<b>Screen to Show:</b> " + screen_info, screen_label)
            ],
            [
                Paragraph("<b>Verbatim Presentation Script (What to Say):</b><br/>" + spoken_text, script_spoken)
            ],
            [
                Paragraph("<b>African Operator Context & Nuance:</b><br/>" + african_nuance, african_context_style)
            ]
        ]
        if checkpoint_q:
            box_data.append([
                Paragraph("<b>Engagement Checkpoint (Ask Client):</b> " + checkpoint_q, body_bold)
            ])

        b_table = Table(box_data, colWidths=[523])
        b_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
            ('BOX', (0, 0), (-1, -1), 1, colors.HexColor("#CBD5E1")),
            ('LINELEFT', (0, 0), (0, -1), 3.5, colors.HexColor("#2563EB")),
            ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
            ('PADDING', (0, 0), (-1, -1), 5.5),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ]))
        return b_table

    # ---------------------------------------------------------
    # DETAILED SCRIPT PHASES
    # ---------------------------------------------------------
    story.append(Paragraph("2. Minute-by-Minute Live Presentation Script", section_heading))

    # Phase 1
    story.append(add_phase_box(
        "Phase 1: [00:00 - 03:00] Welcome, Rapport & Discovery Alignment",
        "Landing Page or Video Call Full Screen (Camera ON)",
        "\"Good morning / Good afternoon everyone. Thank you for taking the time to connect with us today.<br/><br/>"
        "Before diving into the platform, let me briefly align on the context. Operating telecommunications and broadband infrastructure across growing African markets involves very distinct operational realities: frequent fiber cuts from civil works, high cost of physical technician dispatches across dense urban traffic, power grid fluctuations triggering spurious alarms, and high churn in competitive markets.<br/><br/>"
        "Our platform, <b>Jeebr</b>, is engineered to shift operations from <b>reactive firefighting</b> to <b>autonomous, predictive intelligence</b>.<br/><br/>"
        "Over the next 25 minutes, I'll walk you through how our AI predicts network degradation before subscribers notice it, automates remote self-healing to cut fuel and truck-roll costs, and plugs billing leakage. We will reserve the final 5 minutes for your questions.\"",
        "African operators deal with acute infrastructure pressure and high truck-roll expenses. Acknowledging their local operating environment immediately earns trust and shows domain empathy.",
        "\"Before we get underway, does this agenda address the key operational challenges your team is currently tackling?\""
    ))
    story.append(Spacer(1, 8))

    # Phase 2
    story.append(add_phase_box(
        "Phase 2: [03:00 - 06:00] Platform Architecture & Positioning",
        "Landing Page (Feature Section)",
        "\"To give you a quick architectural overview: Jeebr sits directly between your network telemetry layer and your operational teams.<br/><br/>"
        "Whether your network runs on <b>Huawei, ZTE, Nokia, or Fiberhome OLTs</b>, standard BRAS, or TR-069 ACS servers, Jeebr ingests real-time telemetry—optical dBm levels, packet loss, power flap events, and billing records.<br/><br/>"
        "It processes these inputs through specialized AI modules: Predictive Assurance, Autonomous Orchestration, Field Operations Tracking, Churn Retention, and Revenue Assurance—backed by strict Human-in-the-Loop governance so your engineering leadership retains complete authority.\"",
        "Most African telcos operate mixed multi-vendor networks (e.g. Huawei legacy + ZTE expansion). Reassuring them that Jeebr is vendor-neutral and non-intrusive eliminates replacement fears."
    ))
    story.append(Spacer(1, 8))

    # Phase 3
    story.append(add_phase_box(
        "Phase 3: [06:00 - 10:00] Executive Cockpit (Single Pane of Glass)",
        "URL: /cockpit (Show Metric Cards, Network Health Index, Risk Topology)",
        "\"What you see here is the <b>Executive Cockpit</b>—the command center built for C-Level leadership, Head of NOC, and Customer Experience heads.<br/><br/>"
        "<i>(Pro tip: Note that this sandbox is loaded with benchmark metro data to showcase live telemetry at scale; in production, this reflects your exact city nodes.)</i><br/><br/>"
        "Instead of monitoring 5 disjointed consoles, leadership gains complete operational clarity in one view:<br/>"
        "• <b>Network Health Index:</b> Calculated in real-time from optical telemetry across all operational clusters.<br/>"
        "• <b>Predicted At-Risk Subscribers:</b> Accounts exhibiting early behavioral and connection red flags.<br/>"
        "• <b>Identified Revenue Leakage:</b> Billing and speed discrepancies requiring remediation.<br/><br/>"
        "Down here, our <b>AI Risk Topology</b> correlates optical degradation with subscriber density. When a feeder cable or distribution point starts degrading, the platform flags it immediately—before customer call queues explode.\"",
        "African leadership often struggles with visibility across siloed OSS/BSS tools. Highlighting a consolidated single pane of glass speaks directly to CXO pain points."
    ))
    story.append(Spacer(1, 8))

    # Phase 4
    story.append(add_phase_box(
        "Phase 4: [10:00 - 15:00] Predictive Service Assurance & NOC",
        "URL: /assurance (Select an at-risk node, open Explainability Inspector)",
        "\"Let's now step into the boots of your NOC Engineering team. This is <b>Predictive Service Assurance</b>.<br/><br/>"
        "Traditionally, how do operators discover a fiber issue? A subscriber calls to complain, or dozens of angry tickets arrive after an outage.<br/><br/>"
        "Jeebr completely reverses this model. Look at this node: our model flagged an <b>89% risk probability</b> of service degradation within 4 to 6 hours.<br/><br/>"
        "Clicking on the <b>Explainability Inspector</b> reveals the exact telemetry signals:<br/>"
        "• <b>Rx Optical Power degradation:</b> dropping below -27 dBm threshold.<br/>"
        "• <b>Micro-flapping frequency:</b> multiple reconnects detected over the last 90 minutes.<br/>"
        "• <b>Customer impact:</b> 142 enterprise and residential subscribers on this PON port.<br/><br/>"
        "Your NOC can dispatch a technician to inspect the splice or reroute traffic <i>before</i> customer service is impacted.\"",
        "Fiber cuts due to road construction, trenching, or cable theft are prevalent in African cities. Catching bending or attenuation loss early prevents critical trunk collapses.",
        "\"Does your NOC currently track pre-outage optical degradation, or do you primarily rely on alarm thresholds once lines go down?\""
    ))
    story.append(Spacer(1, 8))

    # Phase 5
    story.append(add_phase_box(
        "Phase 5: [15:00 - 19:00] Autonomous Orchestration & Field Force GIS",
        "URL: /orchestration then navigate to /field-operations",
        "\"Once an anomaly is identified, what happens next? This is where <b>Autonomous OSS/BSS Orchestration</b> comes in.<br/><br/>"
        "Every incident is categorized by root cause:<br/>"
        "• <b>Tier-1 Auto-Remediation:</b> If the issue is Wi-Fi channel congestion, DHCP exhaustion, or a firmware hang, Jeebr executes a remote TR-069 reboot or BRAS profile sync. Resolution time: under 3 minutes, at zero field dispatch cost.<br/>"
        "• <b>Tier-2 Physical Dispatch:</b> If telemetry indicates a physical fiber bend or cut, the system generates a field ticket and assigns it to the nearest available technician.<br/><br/>"
        "<i>(Switch to Field Operations Map)</i><br/>"
        "Here on the live GIS map, dispatchers see technician locations, active repair jobs, and real-time SLA breach timers. This cuts hours of phone calls and substantially reduces fuel and vehicle OPEX.\"",
        "Traffic congestion (e.g. Lagos, Nairobi, Johannesburg) makes physical truck rolls extremely slow and expensive. Automating remote fixes directly preserves operating margins."
    ))
    story.append(Spacer(1, 8))

    # Phase 6
    story.append(add_phase_box(
        "Phase 6: [19:00 - 23:00] Churn Retention AI & Revenue Assurance",
        "URL: /churn followed by /revenue",
        "\"Now let's examine bottom-line protection: <b>Churn Retention & Revenue Assurance</b>.<br/><br/>"
        "<i>(In Churn Prediction)</i> In competitive African broadband markets, customer acquisition cost is steep. Losing high-ARPU enterprise or residential subscribers hurts EBITDA. Our Churn AI combines repeated ticket counts, bandwidth throttling signals, and billing payment delays.<br/>"
        "For this at-risk subscriber, Jeebr triggers an automated <b>Next-Best-Action</b>—such as a loyalty bandwidth boost or proactive outreach.<br/><br/>"
        "<i>(Switch to Revenue Assurance)</i> A persistent issue for growing ISPs is OSS/BSS mismatch—where a subscriber is provisioned for 100 Mbps in the AAA/Radius server, but billed for only 30 Mbps in the ledger. Or unbilled add-on boosts.<br/>"
        "Jeebr audits ledgers against network provisioning continuously, pinpointing exact invoice IDs and recoverable revenue with a single click.\"",
        "Revenue leakage from manual provisioning and billing reconciliation errors is a documented 3%–7% drag on African ISP revenues. This module delivers immediate ROI."
    ))
    story.append(Spacer(1, 8))

    # Phase 7
    story.append(add_phase_box(
        "Phase 7: [23:00 - 25:00] Enterprise Governance & Human-in-the-Loop",
        "URL: /governance (Show Pending Approvals & Immutable Audit Trail)",
        "\"A critical question telco leadership always asks: <b>Can the AI trigger unapproved changes on our live network or billing ledgers?</b><br/><br/>"
        "The answer is <b>absolutely not</b>. We enforce enterprise <b>Human-in-the-Loop Governance</b>.<br/><br/>"
        "Every high-impact action—whether dispatching a crew, crediting an invoice, or resetting an OLT port—enters this Governance Queue. An authorized domain lead must review and click 'Approve'. Every action is recorded in an <b>Immutable Audit Trail</b> with timestamps and justification for complete regulatory and internal audit compliance.\"",
        "Telco CTOs and CFOs require strict compliance, SOX-readiness, and non-repudiation before allowing AI automation into core operations."
    ))
    story.append(Spacer(1, 8))

    # Phase 8
    story.append(add_phase_box(
        "Phase 8: [25:00 - 27:00] Customer 360 & Self-Care Touchpoints",
        "URL: /customer360 or /portal/customer",
        "\"From the subscriber's viewpoint: when they interact with customer care or your WhatsApp bot, agents have full visibility in the <b>Customer 360</b> console.<br/><br/>"
        "Agents see live optical power, active Wi-Fi devices, open tickets, and real-time field engineer ETA. Subscribers can track their technician on mobile via SMS or WhatsApp, drastically reducing repetitive inbound call volumes.\"",
        "WhatsApp is the dominant communication channel across Africa. Mentioning seamless self-care tracking resonates strongly with modern telco CX teams."
    ))
    story.append(Spacer(1, 8))

    # Phase 9
    story.append(add_phase_box(
        "Phase 9: [27:00 - 30:00] Summary, Next Steps & 30-Day Pilot Proposal",
        "Return to Executive Cockpit / Video Call Full Screen (Camera ON)",
        "\"To summarize: deploying Jeebr delivers three measurable business outcomes for your network:<br/>"
        "1. <b>Up to 40% reduction in MTTR</b> via predictive assurance and automated triage.<br/>"
        "2. <b>Substantial OPEX savings</b> by eliminating unnecessary physical truck rolls.<br/>"
        "3. <b>Immediate top-line recovery</b> by stopping churn and plugging OSS/BSS billing leakage.<br/><br/>"
        "Our standard engagement begins with a <b>4-Week Risk-Free Pilot</b> on a designated regional cluster (5,000 to 20,000 subscribers) running in parallel with your current systems without service disruption.<br/><br/>"
        "I'd love to open the floor for any questions and discuss how this fits into your upcoming quarterly objectives.\"",
        "Closing with a concrete, low-friction next step (a 4-week non-intrusive pilot) moves the conversation from a theoretical presentation to an actionable commercial engagement."
    ))
    story.append(Spacer(1, 10))

    # ---------------------------------------------------------
    # FAQ & OBJECTION HANDLING
    # ---------------------------------------------------------
    story.append(Paragraph("3. African Client FAQ & Objection Handling Guide", section_heading))

    faqs = [
        ("Q1: How does Jeebr integrate with our existing Huawei / ZTE OLTs and legacy BSS?",
         "<b>Winning Answer:</b> Jeebr is completely vendor-agnostic and non-intrusive. On the network side, we ingest standard SNMP v2/v3, Syslog, TR-069 ACS telemetry, and Kafka streams. On the BSS/billing side, we connect via REST APIs, webhook connectors, or database staging tables. We do not require rip-and-replace; Jeebr operates as an intelligent overlay on top of your existing infrastructure."),

        ("Q2: We experience frequent municipal power outages and generator flaps. Will that cause false alarm storms?",
         "<b>Winning Answer:</b> Our topology engine specifically addresses this through correlated clustering. When a local power substation or backhaul generator drops, all connected ONTs send 'Last Gasp' signals simultaneously. Rather than triggering hundreds of individual fiber break alarms, Jeebr aggregates them into a single 'Power Flap Event', preventing false technician dispatches and saving valuable fuel OPEX."),

        ("Q3: Can Jeebr be deployed On-Premises or in a Private Cloud due to Data Sovereignty regulations?",
         "<b>Winning Answer:</b> Yes, 100%. Jeebr is containerized using Docker and Kubernetes. We support on-premises deployment in your local data center, private cloud environments, or dedicated regional cloud VPCs (such as AWS Cape Town or Azure South Africa) to maintain strict compliance with your national telecommunications regulatory authority."),

        ("Q4: What is required from our team to launch a Proof of Concept (PoC)?",
         "<b>Winning Answer:</b> The setup is very lightweight. We require read-only SNMP/telemetry access for a single pilot cluster (typically 10 to 20 OLT PON ports or 5,000 subscribers) and anonymized billing sample records. The pilot environment is configured within 5 to 7 business days, and we benchmark predictive accuracy over a 4-week evaluation window.")
    ]

    faq_flowables = []
    for q, a in faqs:
        faq_flowables.append(Paragraph(q, faq_q))
        faq_flowables.append(Paragraph(a, faq_a))

    faq_table = Table([[faq_flowables]], colWidths=[523])
    faq_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
        ('BOX', (0, 0), (-1, -1), 1, colors.HexColor("#CBD5E1")),
        ('LINELEFT', (0, 0), (0, -1), 3.5, colors.HexColor("#0D9488")),
        ('PADDING', (0, 0), (-1, -1), 8),
    ]))
    story.append(faq_table)
    story.append(Spacer(1, 10))

    # ---------------------------------------------------------
    # PRESENTER CHECKLIST
    # ---------------------------------------------------------
    story.append(Paragraph("4. Presenter Demo Day Execution Checklist", section_heading))
    
    checklist_text = """
    <b>1. Audio & Latency Management:</b> Keep your pace deliberate and calm. On international calls across regions, latency can cause clipping. Pause 2 seconds after asking questions.<br/>
    <b>2. Pre-Load Browser Tabs:</b> Open <code>/cockpit</code>, <code>/assurance</code>, <code>/orchestration</code>, <code>/field-operations</code>, <code>/churn</code>, and <code>/revenue</code> in separate browser tabs beforehand to eliminate loading lag.<br/>
    <b>3. Highlight Business ROI:</b> Frame every feature in terms of OPEX savings (fuel, truck-rolls), MTTR reduction, and subscriber churn prevention rather than purely algorithmic details.<br/>
    <b>4. Handle Sample Data Confidently:</b> If the client notices Mumbai/Kolkata demo data, state smoothly: <i>'This sandbox uses benchmark metropolitan telemetry to demonstrate high-density loads. In deployment, it connects directly to your local GIS and topology.'</i>
    """
    chk_table = Table([[Paragraph(checklist_text, body_style)]], colWidths=[523])
    chk_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor("#FEF3C7")),
        ('BOX', (0, 0), (-1, -1), 1, colors.HexColor("#F59E0B")),
        ('LINELEFT', (0, 0), (0, -1), 3.5, colors.HexColor("#D97706")),
        ('PADDING', (0, 0), (-1, -1), 8),
    ]))
    story.append(chk_table)

    # Build the document
    doc.build(story, canvasmaker=NumberedCanvas)
    print(f"PDF successfully built: {filename}")

if __name__ == '__main__':
    target_path = r"c:\Users\Lenovo\Documents\GitHub\Jeebr_African_Client_Demo_Script_30Min.pdf"
    build_pdf(target_path)
