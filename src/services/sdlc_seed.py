"""
SDLC Sample Data Seeder
Pre-populates the tracker and testing service with demo projects
so the UI has data to show without needing real Git repos.
"""
from datetime import datetime, timedelta

from src.services.sdlc_tracker import SDLCTracker, SDLCPhaseType, PhaseStatus, PhaseProgress
from src.services.testing_service import TestingService, ClassTestResult, TestStatus


# ── Helper ────────────────────────────────────────────────────────────────────

def _ago(days: float) -> datetime:
    return datetime.now() - timedelta(days=days)


def _completed_phase(phase: SDLCPhaseType, start_ago: float, end_ago: float,
                     tasks_done: int = 5, tasks_total: int = 5) -> PhaseProgress:
    start = _ago(start_ago)
    end   = _ago(end_ago)
    return PhaseProgress(
        phase=phase,
        status=PhaseStatus.COMPLETED,
        progress_percent=100.0,
        start_date=start,
        end_date=end,
        duration_days=(end - start).total_seconds() / 86400,
        tasks_completed=tasks_done,
        tasks_total=tasks_total,
    )


def _live_phase(phase: SDLCPhaseType, start_ago: float,
                progress: float, tasks_done: int, tasks_total: int,
                notes: str = "") -> PhaseProgress:
    return PhaseProgress(
        phase=phase,
        status=PhaseStatus.IN_PROGRESS,
        progress_percent=progress,
        start_date=_ago(start_ago),
        tasks_completed=tasks_done,
        tasks_total=tasks_total,
        notes=notes,
    )


def _pending_phase(phase: SDLCPhaseType) -> PhaseProgress:
    return PhaseProgress(
        phase=phase,
        status=PhaseStatus.NOT_STARTED,
        progress_percent=0.0,
    )


# ── Seed ─────────────────────────────────────────────────────────────────────

def seed_sample_projects(tracker: SDLCTracker, testing_service: TestingService) -> int:
    """Insert demo projects into the in-memory tracker.

    Returns the number of projects inserted (skips if already seeded).
    """
    if tracker.get_all_projects():
        return 0  # already seeded

    count = 0

    # ── Project 1: Claims Portal — currently in TESTING ──────────────────────
    p1 = tracker.create_project(
        name="Claims Portal v3",
        description="Customer-facing claims submission and tracking portal. Microservices architecture on AWS.",
        repositories=[
            {"type": "application",   "url": "https://github.com/aura/claims-portal",   "local_path": "", "branch": "main", "technologies": ["TypeScript", "React"], "file_count": 142},
            {"type": "infrastructure","url": "https://github.com/aura/claims-infra",    "local_path": "", "branch": "main", "technologies": ["Terraform", "AWS"],     "file_count": 38},
            {"type": "devops",        "url": "https://github.com/aura/claims-devops",   "local_path": "", "branch": "main", "technologies": ["GitHub Actions"],       "file_count": 12},
        ]
    )
    # Overwrite auto-created Requirements phase with a completed one
    p1.phase_history = [
        _completed_phase(SDLCPhaseType.REQUIREMENTS, 45, 38, 6, 6),
        _completed_phase(SDLCPhaseType.DESIGN,       38, 28, 8, 8),
        _completed_phase(SDLCPhaseType.DEVELOPMENT,  28, 6,  18, 18),
        _live_phase(SDLCPhaseType.TESTING, 6, 62.0, 5, 8, "Unit tests complete, running integration suite"),
    ]
    p1.current_phase = SDLCPhaseType.TESTING
    p1.metadata["analysis"] = {
        "demo": True,
        "total_classes": 34,
        "total_functions": 187,
        "total_tests": 96,
        "total_lines": 12840,
        "test_coverage_percent": 78.4,
    }
    count += 1

    # Seed a unit-test session for Claims Portal so SubTrack shows live data
    unit_classes = [
        ClassTestResult("ClaimService",     "src/services/ClaimService.ts",     tests_total=12, tests_passed=12, status=TestStatus.PASSED),
        ClassTestResult("PolicyValidator",  "src/validators/PolicyValidator.ts", tests_total=8,  tests_passed=8,  status=TestStatus.PASSED),
        ClassTestResult("UserController",   "src/controllers/UserController.ts", tests_total=10, tests_passed=9,  tests_failed=1, status=TestStatus.FAILED),
        ClassTestResult("NotificationSvc",  "src/services/NotificationSvc.ts",  tests_total=6,  status=TestStatus.RUNNING),
        ClassTestResult("ReportGenerator",  "src/reports/ReportGenerator.ts",   tests_total=7,  status=TestStatus.PENDING),
        ClassTestResult("AuditLogger",      "src/utils/AuditLogger.ts",         tests_total=5,  status=TestStatus.PENDING),
    ]
    for cls in unit_classes:
        cls.file_path = cls.file_path  # already set
    session1 = testing_service.create_test_session(p1.id, test_type="unit", classes_to_test=unit_classes)
    session1.status = "running"
    session1.started_at = _ago(0.01)
    session1.current_class = "NotificationSvc"
    session1.tests_completed = 30
    session1.tests_passed = 29
    session1.tests_failed = 1
    p1.metadata["active_session_id"]   = session1.id
    p1.metadata["active_session_type"] = "unit"

    # Regression session (pre-populated, completed)
    reg_stops = [
        ClassTestResult("Login Flow",       "tests/regression/login.py",       tests_total=4, tests_passed=4, status=TestStatus.PASSED),
        ClassTestResult("Submit Claim",     "tests/regression/submit_claim.py",tests_total=6, tests_passed=6, status=TestStatus.PASSED),
        ClassTestResult("PDF Generation",   "tests/regression/pdf_gen.py",     tests_total=3, tests_passed=2, tests_failed=1, status=TestStatus.FAILED),
        ClassTestResult("Email Alerts",     "tests/regression/email.py",       tests_total=2, tests_passed=2, status=TestStatus.PASSED),
    ]
    reg_session = testing_service.create_test_session(p1.id, test_type="regression", classes_to_test=reg_stops)
    reg_session.status = "completed"
    reg_session.completed_at = _ago(1)
    p1.metadata["regression_session_id"] = reg_session.id

    # ── Project 2: Policy Engine — currently in DEVELOPMENT ──────────────────
    p2 = tracker.create_project(
        name="Policy Engine API",
        description="Core underwriting policy rule engine. Python FastAPI backend with RDF knowledge graph.",
        repositories=[
            {"type": "application",   "url": "https://github.com/aura/policy-engine", "local_path": "", "branch": "develop", "technologies": ["Python", "FastAPI"], "file_count": 87},
            {"type": "devops",        "url": "https://github.com/aura/policy-devops", "local_path": "", "branch": "main",    "technologies": ["Docker", "K8s"],      "file_count": 21},
        ]
    )
    p2.phase_history = [
        _completed_phase(SDLCPhaseType.REQUIREMENTS, 60, 50, 5, 5),
        _completed_phase(SDLCPhaseType.DESIGN,       50, 35, 7, 7),
        _live_phase(SDLCPhaseType.DEVELOPMENT, 35, 45.0, 9, 20, "API endpoints 9/20 complete"),
    ]
    p2.current_phase = SDLCPhaseType.DEVELOPMENT
    p2.metadata["analysis"] = {
        "demo": True,
        "total_classes": 21,
        "total_functions": 94,
        "total_tests": 38,
        "total_lines": 6210,
        "test_coverage_percent": 55.2,
    }
    count += 1

    # ── Project 3: Customer Portal — full SDLC complete ───────────────────────
    p3 = tracker.create_project(
        name="Customer Portal 2.0",
        description="Self-service insurance portal. Fully shipped — now in maintenance.",
        repositories=[
            {"type": "application", "url": "https://github.com/aura/customer-portal", "local_path": "", "branch": "main", "technologies": ["React", "Node.js"], "file_count": 203},
        ]
    )
    p3.phase_history = [
        _completed_phase(SDLCPhaseType.REQUIREMENTS, 120, 110, 5, 5),
        _completed_phase(SDLCPhaseType.DESIGN,       110,  95, 8, 8),
        _completed_phase(SDLCPhaseType.DEVELOPMENT,   95,  60, 22, 22),
        _completed_phase(SDLCPhaseType.TESTING,        60,  30, 14, 14),
        _completed_phase(SDLCPhaseType.DEPLOYMENT,     30,  14, 6,  6),
        _live_phase(SDLCPhaseType.MAINTENANCE, 14, 80.0, 4, 5, "Monitoring dashboards active"),
    ]
    p3.current_phase = SDLCPhaseType.MAINTENANCE
    p3.metadata["analysis"] = {
        "demo": True,
        "total_classes": 51,
        "total_functions": 278,
        "total_tests": 144,
        "total_lines": 19430,
        "test_coverage_percent": 91.3,
    }
    # Playwright E2E session (completed)
    e2e_stops = [
        ClassTestResult("Login Page",        "e2e/login.spec.ts",       tests_total=5, tests_passed=5, status=TestStatus.PASSED),
        ClassTestResult("Dashboard",         "e2e/dashboard.spec.ts",   tests_total=8, tests_passed=8, status=TestStatus.PASSED),
        ClassTestResult("Policy View",       "e2e/policy.spec.ts",      tests_total=6, tests_passed=6, status=TestStatus.PASSED),
        ClassTestResult("Claims Submit",     "e2e/claims.spec.ts",      tests_total=9, tests_passed=8, tests_failed=1, status=TestStatus.FAILED),
        ClassTestResult("Payment Flow",      "e2e/payment.spec.ts",     tests_total=4, tests_passed=4, status=TestStatus.PASSED),
    ]
    e2e_session = testing_service.create_test_session(p3.id, test_type="integration", classes_to_test=e2e_stops)
    e2e_session.status = "completed"
    e2e_session.started_at  = _ago(20)
    e2e_session.completed_at = _ago(19.9)
    e2e_session.tests_passed = sum(c.tests_passed for c in e2e_stops)
    e2e_session.tests_failed = sum(c.tests_failed for c in e2e_stops)
    e2e_session.tests_completed = len(e2e_stops)
    p3.metadata["e2e_session_id"] = e2e_session.id
    count += 1

    # ── Project 4: Fraud Detection — early Requirements phase ────────────────
    p4 = tracker.create_project(
        name="Fraud Detection ML",
        description="ML-based fraud detection pipeline. Kafka + Python. Currently in requirements gathering.",
        repositories=[
            {"type": "application", "url": "https://github.com/aura/fraud-ml", "local_path": "", "branch": "main", "technologies": ["Python", "Kafka", "ML"], "file_count": 14},
        ]
    )
    p4.phase_history = [
        _live_phase(SDLCPhaseType.REQUIREMENTS, 5, 35.0, 3, 8, "Stakeholder interviews in progress"),
    ]
    p4.current_phase = SDLCPhaseType.REQUIREMENTS
    count += 1

    return count
