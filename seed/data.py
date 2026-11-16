"""Demo content (spec section 12). Pure data: no database access.

Every address is on a reserved test domain and every password is demo-only. The runner in
``seed/demo.py`` turns these specs into rows by calling the real services.
"""

import os
from dataclasses import dataclass, field
from typing import Literal

# Demo-only password shared by every demo account. Override with the DEMO_PASSWORD environment variable
# (it must satisfy the normal password policy). The seed never runs in production.
DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD") or "DemoPass#2026"
DEMO_DOMAIN = "demo.example.com"
CANDIDATE_DOMAIN = "candidates.demo.example.com"

# --------------------------------------------------------------------------- accounts


@dataclass(frozen=True)
class Account:
    key: str
    name: str
    email: str


PLATFORM_ADMIN = Account("sa", "Daily Super Admin", f"superadmin.daily@platform.{DEMO_DOMAIN}")

# Org 1 and 2 (the organization names come from DEMO_ORG_NAMES; org 1 uses the vendor name).
ORG1_ADMIN = Account("o1_admin", "Asha Menon", f"admin@one.{DEMO_DOMAIN}")
ORG1_HR = (
    Account("o1_hr1", "Hana Recruiter", f"hana@one.{DEMO_DOMAIN}"),
    Account("o1_hr2", "Ravi Kumar", f"ravi@one.{DEMO_DOMAIN}"),
    Account("o1_hr3", "Meera Iyer", f"meera@one.{DEMO_DOMAIN}"),
    Account("o1_hr4", "Tom Becker", f"tom@one.{DEMO_DOMAIN}"),
)
ORG2_ADMIN = Account("o2_admin", "Walter Acme", f"admin@acme.{DEMO_DOMAIN}")
ORG2_HR = (Account("o2_hr1", "Lena Fischer", f"lena@acme.{DEMO_DOMAIN}"),)

ORG1_SLUG = "demo-one"
ORG2_SLUG = "acme-demo"
ORG2_NAME = "Acme Demo Corp"

# Flags switched off for organization 2 (organization 1 has every flag on).
ORG2_FLAGS_OFF = (
    "camera_proctoring",
    "csv_bulk_invite",
    "reports_chat",
    "ai_question_builder",
    "ai_scoring",
    "question_bank",
)

# --------------------------------------------------------------------------- policy documents

CONSENT_HTML = (
    "<h2>Consent to proctoring</h2>"
    "<p>This assessment is proctored. Your answers, browser activity and, when your organization "
    "requires it, periodic camera snapshots are recorded and shown to the hiring team.</p>"
    "<p>By continuing you agree to this processing.</p>"
)
PRIVACY_HTML = (
    "<h2>Privacy notice</h2>"
    "<p>We store your name, email address, answers and proctoring evidence only for this hiring "
    "process. Data is deleted after the retention period set by the organization.</p>"
)

BRANDING = {"primary_color": "#0F4C5C", "accent_color": "#9A3412", "background_style": "soft_gray"}

# --------------------------------------------------------------------------- assessments


@dataclass(frozen=True)
class Q:
    type: Literal["single_choice", "multi_choice", "fill_blank", "code_gap", "short_answer"]
    prompt: str
    marks: float
    options: tuple[tuple[str, bool], ...] = ()
    key: dict | None = None
    model_answer: str | None = None
    language: str | None = None


@dataclass(frozen=True)
class AssessmentSpec:
    key: str
    org: int
    owner: str  # account key
    title: str
    description: str
    duration_minutes: int
    pass_mark_percent: int
    instructions: str
    questions: tuple[Q, ...]
    snapshot_interval_seconds: int = 60
    extra_v2: tuple[Q, ...] = ()  # questions added in a second published version


JAVA = AssessmentSpec(
    key="java",
    org=1,
    owner="o1_hr1",
    title="Java Developer Assessment",
    description="Core Java, collections and code reading for backend hires.",
    duration_minutes=45,
    pass_mark_percent=60,
    instructions="<p>Answer every question. You may move back and forth. The timer cannot be paused.</p>",
    questions=(
        Q(
            "single_choice",
            "<p>Which memory area stores objects created with <code>new</code>?</p>",
            2,
            (("Heap", True), ("Stack", False), ("Metaspace", False), ("Program counter", False)),
        ),
        Q(
            "multi_choice",
            "<p>Select every collection that keeps insertion order.</p>",
            3,
            (("ArrayList", True), ("LinkedList", True), ("HashSet", False), ("LinkedHashSet", True)),
            key={"partial": True},
        ),
        Q(
            "fill_blank",
            "<p>The ____ keyword stops a variable from being reassigned.</p>",
            2,
            key={"blanks": [{"accepted": ["final"]}], "case_sensitive": False},
        ),
        Q(
            "code_gap",
            "<p>Complete the entry point: <code>public ____ void main(String[] args)</code></p>",
            3,
            key={"gaps": [{"accepted": ["static"]}], "case_sensitive": True},
            language="java",
        ),
        Q(
            "short_answer",
            "<p>Explain the difference between <code>==</code> and <code>equals()</code> for objects.</p>",
            4,
            key={"key_points": ["reference equality", "value equality", "override equals and hashCode"]},
            model_answer="== compares references; equals() compares logical value and should be "
            "overridden together with hashCode().",
        ),
    ),
    extra_v2=(
        Q(
            "single_choice",
            "<p>Which functional interface does <code>Stream.map</code> accept?</p>",
            2,
            (("Function", True), ("Supplier", False), ("Consumer", False), ("Runnable", False)),
        ),
    ),
)

QA = AssessmentSpec(
    key="qa",
    org=1,
    owner="o1_hr2",
    title="QA Engineer Assessment",
    description="Test design, techniques and bug reporting.",
    duration_minutes=40,
    pass_mark_percent=55,
    instructions="<p>Read each scenario carefully. Short answers are reviewed by a person.</p>",
    questions=(
        Q(
            "single_choice",
            "<p>What is the main goal of regression testing?</p>",
            2,
            (
                ("Confirm existing behaviour still works after a change", True),
                ("Find new features to build", False),
                ("Measure page load time", False),
                ("Review source code style", False),
            ),
        ),
        Q(
            "multi_choice",
            "<p>Select the black-box techniques.</p>",
            3,
            (
                ("Equivalence partitioning", True),
                ("Boundary value analysis", True),
                ("Statement coverage", False),
                ("Decision table testing", True),
            ),
        ),
        Q(
            "fill_blank",
            "<p>A test ____ describes the exact steps and expected result of one check.</p>",
            2,
            key={"blanks": [{"accepted": ["case", "test case"]}], "case_sensitive": False},
        ),
        Q(
            "short_answer",
            "<p>Describe how you would test a login form.</p>",
            5,
            key={"key_points": ["valid and invalid credentials", "lockout", "error messages"]},
            model_answer="Cover valid and invalid credentials, empty fields, lockout after repeated "
            "failures, error message wording and session handling.",
        ),
    ),
)

FRONTEND = AssessmentSpec(
    key="frontend",
    org=1,
    owner="o1_hr3",
    title="Frontend Developer Assessment",
    description="HTML, CSS and React fundamentals.",
    duration_minutes=40,
    pass_mark_percent=60,
    instructions="<p>No external resources. Code gaps are checked automatically.</p>",
    questions=(
        Q(
            "single_choice",
            "<p>Which CSS property controls the space between an element's border and its content?</p>",
            2,
            (("padding", True), ("margin", False), ("gap", False), ("outline", False)),
        ),
        Q(
            "code_gap",
            "<p>Centre a flex child: <code>display: flex; justify-content: ____;</code></p>",
            3,
            key={"gaps": [{"accepted": ["center"]}], "case_sensitive": False},
            language="css",
        ),
        Q(
            "fill_blank",
            "<p>The HTML attribute ____ gives an image its text alternative.</p>",
            2,
            key={"blanks": [{"accepted": ["alt"]}], "case_sensitive": False},
        ),
        Q(
            "multi_choice",
            "<p>Select the built-in React hooks.</p>",
            3,
            (("useState", True), ("useEffect", True), ("useQuery", False), ("useMemo", True)),
        ),
    ),
)

APTITUDE = AssessmentSpec(
    key="aptitude",
    org=2,
    owner="o2_hr1",
    title="General Aptitude Assessment",
    description="Numerical and logical reasoning for graduate hires.",
    duration_minutes=30,
    pass_mark_percent=50,
    instructions="<p>Calculators are not allowed.</p>",
    questions=(
        Q(
            "single_choice",
            "<p>What is 15% of 240?</p>",
            2,
            (("36", True), ("24", False), ("30", False), ("42", False)),
        ),
        Q(
            "multi_choice",
            "<p>Select every prime number.</p>",
            2,
            (("11", True), ("15", False), ("17", True), ("21", False)),
        ),
        Q(
            "fill_blank",
            "<p>Next number in the series 2, 4, 8, 16: ____</p>",
            2,
            key={"blanks": [{"accepted": ["32"]}], "case_sensitive": False},
        ),
        Q(
            "single_choice",
            "<p>All roses are flowers. Some flowers fade quickly. Which statement must be true?</p>",
            2,
            (
                ("All roses are flowers", True),
                ("All flowers are roses", False),
                ("Some roses fade quickly", False),
                ("No rose fades quickly", False),
            ),
        ),
    ),
)

ASSESSMENTS = (JAVA, QA, FRONTEND, APTITUDE)

# --------------------------------------------------------------------------- roles, invites, attempts


@dataclass(frozen=True)
class EventSpec:
    type: str
    duration_ms: int | None = None
    snapshot: bool = False  # attach a generated placeholder image to this event


Outcome = Literal["submitted", "in_progress", "invited", "expired", "revoked", "help_open"]


@dataclass(frozen=True)
class InviteSpec:
    name: str
    outcome: Outcome
    version_no: int = 1
    correct: str = ""  # one char per question in order: 1 right, 0 wrong, - skip; missing = unanswered
    short_marks: dict[int, float] = field(default_factory=dict)  # position (1-based) -> manual marks
    events: tuple[EventSpec, ...] = ()
    dismiss: tuple[str, ...] = ()  # event types the reviewer dismisses
    decision: Literal["approved", "rejected"] | None = None
    override: tuple[str, str] | None = None  # (decision, reason) by the organization admin
    camera_exempt: bool = False
    time_multiplier: float | None = None
    extra_minutes: int | None = None
    help_first: bool = False  # asks for help before starting; approved with camera exemption + 10 min
    help_reason: str = "camera_not_working"
    extra_time_grant: int | None = None  # reviewer adds minutes to the running attempt
    expires_in_hours: int | None = None
    days_ago: int = 0

    @property
    def email(self) -> str:
        local = self.name.lower().replace(" ", ".")
        return f"{local}@{CANDIDATE_DOMAIN}"


@dataclass(frozen=True)
class RoleSpec:
    key: str
    org: int
    owner: str
    title: str
    department: str
    level: str
    vacancies: int
    description: str
    assessment: str
    status: Literal["draft", "open", "closed"]
    invites: tuple[InviteSpec, ...] = ()


ROLES = (
    RoleSpec(
        "java",
        1,
        "o1_hr1",
        "Senior Java Developer",
        "Platform",
        "senior",
        2,
        "Own the payments services.",
        "java",
        "open",
        (
            InviteSpec("Asha Rao", "submitted", 1, "1111", {5: 4}, decision="approved", days_ago=12),
            InviteSpec("Brian Mills", "submitted", 1, "1010", {5: 1}, decision="rejected", days_ago=11),
            InviteSpec(
                "Chen Wei",
                "submitted",
                1,
                "1101",
                {5: 2},
                events=(
                    EventSpec("tab_switch", 4000),
                    EventSpec("copy_attempt"),
                    EventSpec("tab_switch", 12000),
                    EventSpec("tab_switch", 3000, snapshot=True),
                ),
                dismiss=("copy_attempt",),
                days_ago=9,
            ),
            InviteSpec(
                "Divya Nair",
                "submitted",
                2,
                "111111",
                {5: 4},
                decision="approved",
                override=("rejected", "Reference check raised a conduct concern."),
                days_ago=6,
            ),
            InviteSpec(
                "Emeka Obi",
                "in_progress",
                2,
                "11",
                events=(EventSpec("window_blur", 2500),),
                extra_time_grant=10,
            ),
            InviteSpec("Farah Khan", "invited", 2, expires_in_hours=20),
            InviteSpec("Gabriel Costa", "revoked", 2),
        ),
    ),
    RoleSpec(
        "qa",
        1,
        "o1_hr2",
        "QA Engineer",
        "Quality",
        "mid",
        1,
        "Automate and own regression suites.",
        "qa",
        "open",
        (
            InviteSpec("Hiro Tanaka", "submitted", 1, "1110", {4: 3}, camera_exempt=True, days_ago=8),
            InviteSpec(
                "Isabel Cruz",
                "submitted",
                1,
                "1100",
                {4: 4},
                decision="approved",
                time_multiplier=1.5,
                extra_minutes=15,
                days_ago=7,
            ),
            InviteSpec("Jon Park", "expired", 1),
            InviteSpec("Kira Novak", "help_open", 1),
            InviteSpec(
                "Liam Ford",
                "submitted",
                1,
                "0110",
                {4: 2},
                help_first=True,
                help_reason="no_camera",
                days_ago=3,
            ),
        ),
    ),
    RoleSpec(
        "frontend",
        1,
        "o1_hr3",
        "Frontend Developer",
        "Web",
        "mid",
        3,
        "Build the candidate-facing apps.",
        "frontend",
        "closed",
        (
            InviteSpec("Maya Ortiz", "submitted", 1, "1000", decision="rejected", days_ago=14),
            InviteSpec(
                "Noah Berg",
                "submitted",
                1,
                "1111",
                events=(EventSpec("multiple_faces", snapshot=True), EventSpec("no_face", 14000)),
                days_ago=13,
            ),
            InviteSpec("Olga Petrova", "invited", 1),  # revoked automatically when the role closes
        ),
    ),
    RoleSpec(
        "writer",
        1,
        "o1_hr4",
        "Technical Writer",
        "Docs",
        "junior",
        1,
        "Write developer documentation.",
        "frontend",
        "draft",
    ),
    RoleSpec(
        "trainee",
        2,
        "o2_hr1",
        "Graduate Trainee",
        "Operations",
        "graduate",
        5,
        "Rotational programme across operations.",
        "aptitude",
        "open",
        (
            InviteSpec("Priya Shah", "submitted", 1, "1111", decision="approved", days_ago=5),
            InviteSpec(
                "Quinn Lee", "submitted", 1, "1011", events=(EventSpec("tab_switch", 3000),), days_ago=4
            ),
            InviteSpec("Rafael Diaz", "invited", 1),
            InviteSpec("Sofia Rossi", "submitted", 1, "0100", decision="rejected", days_ago=2),
        ),
    ),
    RoleSpec(
        "support",
        2,
        "o2_hr1",
        "Support Associate",
        "Customer care",
        "junior",
        2,
        "First line support.",
        "aptitude",
        "closed",
        (InviteSpec("Tariq Aziz", "submitted", 1, "1110", decision="approved", days_ago=1),),
    ),
)

# What a correct seed produces (the tests compare against this).
EXPECTED_COUNTS = {
    "organizations": 2,
    "users": 8,  # daily SA (the break-glass default SA is not counted) + (1 admin + 4 HR) + (1 admin + 1 HR)
    "assessments": 4,
    "assessment_versions": 5,
    "open_roles": 6,
    "invites": 20,
    "attempts": 15,
    "help_requests": 2,
    "decisions": 9,
}

DEMO_ACCOUNTS = (
    (PLATFORM_ADMIN, "super_admin", "platform"),
    (ORG1_ADMIN, "org_admin", "org 1"),
    *((a, "hr_user", "org 1") for a in ORG1_HR),
    (ORG2_ADMIN, "org_admin", "org 2"),
    *((a, "hr_user", "org 2") for a in ORG2_HR),
)
