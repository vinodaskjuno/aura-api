"""
SDLC Tracker Service
Manages SDLC phases and project progress tracking
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from enum import Enum
import uuid


class SDLCPhaseType(Enum):
    """SDLC Phase Types"""
    REQUIREMENTS = "Requirements"
    DESIGN = "Design"
    DEVELOPMENT = "Development"
    TESTING = "Testing"
    DEPLOYMENT = "Deployment"
    MAINTENANCE = "Maintenance"


class PhaseStatus(Enum):
    """Phase Status"""
    NOT_STARTED = "not-started"
    IN_PROGRESS = "in-progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


@dataclass
class PhaseProgress:
    """Progress information for a phase"""
    phase: SDLCPhaseType
    status: PhaseStatus
    progress_percent: float = 0.0
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    target_date: Optional[datetime] = None
    duration_days: Optional[float] = None
    tasks_total: int = 0
    tasks_completed: int = 0
    blockers: List[str] = field(default_factory=list)
    notes: str = ""


@dataclass
class SDLCProject:
    """SDLC Project entity"""
    id: str
    name: str
    description: str
    repositories: List[Dict] = field(default_factory=list)
    current_phase: SDLCPhaseType = SDLCPhaseType.REQUIREMENTS
    phase_history: List[PhaseProgress] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: Dict = field(default_factory=dict)


@dataclass
class PhaseTransition:
    """Phase transition event"""
    project_id: str
    from_phase: SDLCPhaseType
    to_phase: SDLCPhaseType
    timestamp: datetime
    reason: str = ""
    auto_approved: bool = False


class SDLCTracker:
    """SDLC Phase Tracker Service"""

    # Define phase order and transitions
    PHASE_ORDER = [
        SDLCPhaseType.REQUIREMENTS,
        SDLCPhaseType.DESIGN,
        SDLCPhaseType.DEVELOPMENT,
        SDLCPhaseType.TESTING,
        SDLCPhaseType.DEPLOYMENT,
        SDLCPhaseType.MAINTENANCE,
    ]

    # Define prerequisites for each phase
    PHASE_PREREQUISITES = {
        SDLCPhaseType.REQUIREMENTS: [],
        SDLCPhaseType.DESIGN: [
            "Requirements document exists",
            "Stakeholders identified"
        ],
        SDLCPhaseType.DEVELOPMENT: [
            "Design approved",
            "Repository setup complete"
        ],
        SDLCPhaseType.TESTING: [
            "Code developed",
            "Build successful"
        ],
        SDLCPhaseType.DEPLOYMENT: [
            "Tests passed",
            "Code review approved"
        ],
        SDLCPhaseType.MAINTENANCE: [
            "Deployed to production",
            "Documentation complete"
        ],
    }

    def __init__(self):
        self.projects: Dict[str, SDLCProject] = {}

    def create_project(self, name: str, description: str,
                      repositories: List[Dict] = None) -> SDLCProject:
        """Create a new SDLC project

        Args:
            name: Project name
            description: Project description
            repositories: List of repository configurations

        Returns:
            Created SDLCProject
        """
        project_id = str(uuid.uuid4())

        project = SDLCProject(
            id=project_id,
            name=name,
            description=description,
            repositories=repositories or [],
            current_phase=SDLCPhaseType.REQUIREMENTS
        )

        # Initialize phase history with Requirements phase
        req_phase = PhaseProgress(
            phase=SDLCPhaseType.REQUIREMENTS,
            status=PhaseStatus.IN_PROGRESS,
            start_date=datetime.now()
        )
        project.phase_history.append(req_phase)

        self.projects[project_id] = project
        return project

    def get_project(self, project_id: str) -> Optional[SDLCProject]:
        """Get project by ID"""
        return self.projects.get(project_id)

    def get_all_projects(self) -> List[SDLCProject]:
        """Get all projects"""
        return list(self.projects.values())

    def get_current_phase_progress(self, project_id: str) -> Optional[PhaseProgress]:
        """Get current phase progress

        Args:
            project_id: Project ID

        Returns:
            PhaseProgress for current phase
        """
        project = self.get_project(project_id)
        if not project:
            return None

        # Find the current phase in history
        for phase_prog in reversed(project.phase_history):
            if phase_prog.phase == project.current_phase:
                return phase_prog

        return None

    def update_phase_progress(self, project_id: str,
                             progress_percent: Optional[float] = None,
                             tasks_completed: Optional[int] = None,
                             tasks_total: Optional[int] = None,
                             notes: Optional[str] = None) -> bool:
        """Update progress of current phase

        Args:
            project_id: Project ID
            progress_percent: Progress percentage (0-100)
            tasks_completed: Number of completed tasks
            tasks_total: Total number of tasks
            notes: Progress notes

        Returns:
            Success status
        """
        phase_prog = self.get_current_phase_progress(project_id)
        if not phase_prog:
            return False

        if progress_percent is not None:
            phase_prog.progress_percent = min(100.0, max(0.0, progress_percent))

        if tasks_completed is not None:
            phase_prog.tasks_completed = tasks_completed

        if tasks_total is not None:
            phase_prog.tasks_total = tasks_total

        if notes is not None:
            phase_prog.notes = notes

        # Auto-calculate progress from tasks if available
        if phase_prog.tasks_total > 0:
            phase_prog.progress_percent = (
                phase_prog.tasks_completed / phase_prog.tasks_total * 100
            )

        # Update project timestamp
        project = self.get_project(project_id)
        if project:
            project.updated_at = datetime.now()

        return True

    def can_transition_to_phase(self, project_id: str,
                               target_phase: SDLCPhaseType) -> tuple[bool, List[str]]:
        """Check if project can transition to target phase

        Args:
            project_id: Project ID
            target_phase: Target phase to transition to

        Returns:
            Tuple of (can_transition, list_of_missing_prerequisites)
        """
        project = self.get_project(project_id)
        if not project:
            return False, ["Project not found"]

        # Check if target phase is next in sequence
        current_idx = self.PHASE_ORDER.index(project.current_phase)
        target_idx = self.PHASE_ORDER.index(target_phase)

        if target_idx <= current_idx:
            return False, ["Cannot move backwards or to same phase"]

        if target_idx > current_idx + 1:
            return False, ["Cannot skip phases. Move to next phase sequentially."]

        # Check prerequisites
        missing_prerequisites = []
        current_progress = self.get_current_phase_progress(project_id)

        # Current phase should be completed or near completion
        if current_progress and current_progress.progress_percent < 80.0:
            missing_prerequisites.append(
                f"Current phase ({project.current_phase.value}) is not complete enough "
                f"({current_progress.progress_percent:.0f}% complete)"
            )

        # Check target phase prerequisites (this would be customized per project)
        # For now, just return the standard prerequisites
        prerequisites = self.PHASE_PREREQUISITES.get(target_phase, [])
        # In a real system, you'd check if these are actually met
        # For now, we just return them as informational

        can_transition = len(missing_prerequisites) == 0

        return can_transition, missing_prerequisites if not can_transition else []

    def transition_to_phase(self, project_id: str,
                           target_phase: SDLCPhaseType,
                           force: bool = False) -> tuple[bool, str]:
        """Transition project to a new phase

        Args:
            project_id: Project ID
            target_phase: Target phase
            force: Force transition even if prerequisites not met

        Returns:
            Tuple of (success, message)
        """
        project = self.get_project(project_id)
        if not project:
            return False, "Project not found"

        # Check if transition is allowed
        can_transition, reasons = self.can_transition_to_phase(project_id, target_phase)

        if not can_transition and not force:
            return False, f"Cannot transition: {'; '.join(reasons)}"

        # Complete current phase
        current_progress = self.get_current_phase_progress(project_id)
        if current_progress:
            current_progress.status = PhaseStatus.COMPLETED
            current_progress.end_date = datetime.now()
            if current_progress.start_date:
                duration = datetime.now() - current_progress.start_date
                current_progress.duration_days = duration.total_seconds() / 86400

        # Create new phase progress
        new_phase_progress = PhaseProgress(
            phase=target_phase,
            status=PhaseStatus.IN_PROGRESS,
            start_date=datetime.now(),
            progress_percent=0.0
        )

        project.phase_history.append(new_phase_progress)
        project.current_phase = target_phase
        project.updated_at = datetime.now()

        return True, f"Successfully transitioned to {target_phase.value}"

    def get_next_phase(self, project_id: str) -> Optional[SDLCPhaseType]:
        """Get the next phase for a project

        Args:
            project_id: Project ID

        Returns:
            Next SDLCPhaseType or None if already at final phase
        """
        project = self.get_project(project_id)
        if not project:
            return None

        current_idx = self.PHASE_ORDER.index(project.current_phase)
        if current_idx < len(self.PHASE_ORDER) - 1:
            return self.PHASE_ORDER[current_idx + 1]

        return None

    def get_phase_tracker_view(self, project_id: str) -> Dict:
        """Get 'train tracker' style view of phases

        Args:
            project_id: Project ID

        Returns:
            Dictionary with visualization data
        """
        project = self.get_project(project_id)
        if not project:
            return {}

        current_idx = self.PHASE_ORDER.index(project.current_phase)
        next_phase = self.get_next_phase(project_id)

        # Build phase tracker
        phases_view = []
        for idx, phase in enumerate(self.PHASE_ORDER):
            phase_data = {
                'name': phase.value,
                'order': idx,
                'is_current': phase == project.current_phase,
                'is_past': idx < current_idx,
                'is_future': idx > current_idx,
                'status': 'completed' if idx < current_idx else
                         ('in-progress' if idx == current_idx else 'pending')
            }

            # Add progress info if available
            for phase_prog in project.phase_history:
                if phase_prog.phase == phase:
                    phase_data['progress_percent'] = phase_prog.progress_percent
                    phase_data['start_date'] = phase_prog.start_date.isoformat() if phase_prog.start_date else None
                    phase_data['end_date'] = phase_prog.end_date.isoformat() if phase_prog.end_date else None
                    phase_data['duration_days'] = phase_prog.duration_days
                    phase_data['tasks_completed'] = phase_prog.tasks_completed
                    phase_data['tasks_total'] = phase_prog.tasks_total
                    break

            phases_view.append(phase_data)

        return {
            'project_id': project.id,
            'project_name': project.name,
            'current_phase': project.current_phase.value,
            'next_phase': next_phase.value if next_phase else None,
            'phases': phases_view,
            'overall_progress': self._calculate_overall_progress(project)
        }

    def _calculate_overall_progress(self, project: SDLCProject) -> float:
        """Calculate overall project progress across all phases"""
        current_idx = self.PHASE_ORDER.index(project.current_phase)
        total_phases = len(self.PHASE_ORDER)

        # Base progress: completed phases
        completed_progress = (current_idx / total_phases) * 100

        # Add current phase progress
        current_phase_prog = self.get_current_phase_progress(project.id)
        if current_phase_prog:
            current_phase_weight = (1 / total_phases) * 100
            current_contribution = (current_phase_prog.progress_percent / 100) * current_phase_weight
            completed_progress += current_contribution

        return min(100.0, completed_progress)

    def add_blocker(self, project_id: str, blocker: str) -> bool:
        """Add a blocker to current phase

        Args:
            project_id: Project ID
            blocker: Blocker description

        Returns:
            Success status
        """
        phase_prog = self.get_current_phase_progress(project_id)
        if not phase_prog:
            return False

        phase_prog.blockers.append(blocker)
        phase_prog.status = PhaseStatus.BLOCKED

        return True

    def remove_blocker(self, project_id: str, blocker: str) -> bool:
        """Remove a blocker from current phase

        Args:
            project_id: Project ID
            blocker: Blocker description

        Returns:
            Success status
        """
        phase_prog = self.get_current_phase_progress(project_id)
        if not phase_prog:
            return False

        if blocker in phase_prog.blockers:
            phase_prog.blockers.remove(blocker)

        # If no more blockers, set back to in-progress
        if not phase_prog.blockers:
            phase_prog.status = PhaseStatus.IN_PROGRESS

        return True


def main():
    """Test the SDLC tracker"""
    tracker = SDLCTracker()

    # Create a project
    print("=== Creating SDLC Project ===")
    project = tracker.create_project(
        name="Claims Portal Modernization",
        description="Modernize the claims processing portal",
        repositories=[
            {"type": "application", "url": "github.com/org/claims-app"},
            {"type": "config", "url": "github.com/org/claims-infra"}
        ]
    )
    print(f"Project ID: {project.id}")
    print(f"Current Phase: {project.current_phase.value}")

    # Update progress
    print("\n=== Updating Requirements Phase ===")
    tracker.update_phase_progress(
        project.id,
        tasks_completed=8,
        tasks_total=10,
        notes="Requirements gathering 80% complete"
    )

    # Get tracker view
    view = tracker.get_phase_tracker_view(project.id)
    print(f"Overall Progress: {view['overall_progress']:.1f}%")
    print(f"Next Phase: {view['next_phase']}")

    # Try to transition to Development (should fail)
    print("\n=== Attempting to Skip to Development ===")
    success, msg = tracker.transition_to_phase(project.id, SDLCPhaseType.DEVELOPMENT)
    print(f"Success: {success}, Message: {msg}")

    # Transition to Design
    print("\n=== Transitioning to Design Phase ===")
    tracker.update_phase_progress(project.id, progress_percent=100)
    success, msg = tracker.transition_to_phase(project.id, SDLCPhaseType.DESIGN)
    print(f"Success: {success}, Message: {msg}")

    # Show updated tracker
    view = tracker.get_phase_tracker_view(project.id)
    print(f"\nCurrent Phase: {view['current_phase']}")
    print(f"Overall Progress: {view['overall_progress']:.1f}%")
    print(f"\nPhase Timeline:")
    for phase in view['phases']:
        status_icon = "✓" if phase['is_past'] else ("→" if phase['is_current'] else "○")
        print(f"  {status_icon} {phase['name']} ({phase['status']})")


if __name__ == "__main__":
    main()
