"""
Repository Connector for SDLC Ontology System
Handles Git repository cloning and local path scanning
"""
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime
import json


@dataclass
class RepositoryMetadata:
    """Metadata extracted from a repository"""
    repo_type: str  # "application", "config", "devops"
    url: Optional[str] = None
    local_path: str = ""
    branch: str = "main"
    technologies: List[str] = field(default_factory=list)
    file_count: int = 0
    total_lines: int = 0
    file_types: Dict[str, int] = field(default_factory=dict)
    structure: Dict[str, any] = field(default_factory=dict)
    dependencies: List[str] = field(default_factory=list)
    last_synced: str = ""


@dataclass
class CodeFile:
    """Represents a code file in the repository"""
    path: str
    language: str
    size_bytes: int
    line_count: int
    classes: List[str] = field(default_factory=list)
    functions: List[str] = field(default_factory=list)
    imports: List[str] = field(default_factory=list)
    is_test: bool = False


class RepositoryConnector:
    """Connector for repository operations"""

    LANGUAGE_EXTENSIONS = {
        '.py': 'Python',
        '.java': 'Java',
        '.js': 'JavaScript',
        '.ts': 'TypeScript',
        '.jsx': 'React',
        '.tsx': 'React TypeScript',
        '.go': 'Go',
        '.rs': 'Rust',
        '.cpp': 'C++',
        '.c': 'C',
        '.cs': 'C#',
        '.rb': 'Ruby',
        '.php': 'PHP',
        '.swift': 'Swift',
        '.kt': 'Kotlin',
    }

    CONFIG_FILES = {
        'requirements.txt': 'Python',
        'package.json': 'Node.js',
        'pom.xml': 'Java Maven',
        'build.gradle': 'Java Gradle',
        'Cargo.toml': 'Rust',
        'go.mod': 'Go',
        'composer.json': 'PHP',
        'Gemfile': 'Ruby',
    }

    INFRASTRUCTURE_FILES = {
        'Dockerfile': 'Docker',
        'docker-compose.yml': 'Docker Compose',
        'terraform': 'Terraform',
        'kubernetes': 'Kubernetes',
        '.github/workflows': 'GitHub Actions',
        'Jenkinsfile': 'Jenkins',
        '.gitlab-ci.yml': 'GitLab CI',
    }

    def __init__(self, workspace_dir: str = "./data/repos"):
        """Initialize repository connector

        Args:
            workspace_dir: Directory to store cloned repositories
        """
        self.workspace_dir = Path(workspace_dir)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)

    def clone_repository(self, repo_url: str, branch: str = "main",
                        repo_type: str = "application",
                        git_token: Optional[str] = None) -> RepositoryMetadata:
        """Clone a Git repository.

        Supports enterprise GitHub and private repos via a personal access token.
        If cloning fails, returns metadata with the URL recorded but no local files
        so the project is still created successfully.

        Args:
            repo_url:   URL of the Git repository
            branch:     Branch to clone (default: main)
            repo_type:  Type of repository (application/infrastructure/devops)
            git_token:  Optional PAT/token injected into the clone URL for auth
        """
        repo_name = repo_url.rstrip('/').split('/')[-1].replace('.git', '')
        local_path = self.workspace_dir / repo_type / repo_name

        # Embed token into HTTPS URL when provided:  https://TOKEN@host/path
        clone_url = repo_url
        if git_token:
            from urllib.parse import urlparse, urlunparse
            parsed = urlparse(repo_url)
            clone_url = urlunparse(parsed._replace(netloc=f"{git_token}@{parsed.netloc}"))

        if local_path.exists():
            # Already cloned — try a pull, ignore errors (offline / auth)
            try:
                subprocess.run(
                    ['git', 'pull', 'origin', branch],
                    cwd=local_path, check=True, capture_output=True, timeout=60
                )
            except Exception as e:
                print(f"Warning: pull failed for {repo_name}: {e}")
        else:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            # Try the preferred branch first, fall back to 'master' then bare clone
            for attempt_branch in ([branch] + ([] if branch == 'master' else ['master'])):
                try:
                    subprocess.run(
                        ['git', 'clone', '--depth', '1', '-b', attempt_branch,
                         clone_url, str(local_path)],
                        check=True, capture_output=True, timeout=120
                    )
                    break  # success
                except subprocess.CalledProcessError:
                    if local_path.exists():
                        import shutil; shutil.rmtree(local_path, ignore_errors=True)
                    continue
                except Exception as e:
                    print(f"Clone failed for {repo_url}: {e}")
                    break
            else:
                # All branch attempts failed — return metadata without local path
                print(f"Could not clone {repo_url}. Project will be created without local analysis.")
                return RepositoryMetadata(
                    repo_type=repo_type,
                    url=repo_url,
                    local_path="",
                    branch=branch,
                    last_synced=datetime.now().isoformat(),
                )

        if not local_path.exists():
            return RepositoryMetadata(
                repo_type=repo_type,
                url=repo_url,
                local_path="",
                branch=branch,
                last_synced=datetime.now().isoformat(),
            )

        return self.scan_repository(str(local_path), repo_url, branch, repo_type)

    def connect_local_path(self, local_path: str,
                          repo_type: str = "application") -> RepositoryMetadata:
        """Connect to a local repository path

        Args:
            local_path: Path to local repository
            repo_type: Type of repository

        Returns:
            RepositoryMetadata with extracted information
        """
        if not os.path.exists(local_path):
            raise ValueError(f"Path does not exist: {local_path}")

        # Try to detect Git URL if it's a Git repo
        repo_url = None
        try:
            result = subprocess.run(
                ['git', 'config', '--get', 'remote.origin.url'],
                cwd=local_path,
                capture_output=True,
                text=True,
                check=True
            )
            repo_url = result.stdout.strip()
        except:
            pass

        # Detect branch
        branch = "main"
        try:
            result = subprocess.run(
                ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
                cwd=local_path,
                capture_output=True,
                text=True,
                check=True
            )
            branch = result.stdout.strip()
        except:
            pass

        return self.scan_repository(local_path, repo_url, branch, repo_type)

    def scan_repository(self, local_path: str, repo_url: Optional[str] = None,
                       branch: str = "main", repo_type: str = "application") -> RepositoryMetadata:
        """Scan a repository and extract metadata

        Args:
            local_path: Path to repository
            repo_url: URL of repository (optional)
            branch: Branch name
            repo_type: Type of repository

        Returns:
            RepositoryMetadata with all extracted information
        """
        metadata = RepositoryMetadata(
            repo_type=repo_type,
            url=repo_url,
            local_path=local_path,
            branch=branch,
            last_synced=datetime.now().isoformat()
        )

        # Scan directory structure
        file_types = {}
        total_lines = 0
        technologies = set()
        
        # Directories to skip (performance optimization)
        SKIP_DIRS = {
            '.venv', 'venv', 'env', 'ENV',
            '__pycache__', '.pytest_cache',
            'node_modules', '.git', '.github',
            'build', 'dist', 'target', '.tox',
            'htmlcov', '.mypy_cache', '.coverage',
            'site-packages'
        }

        print(f"Scanning repository: {local_path}")
        file_count = 0
        
        for root, dirs, files in os.walk(local_path):
            # Skip hidden and common ignore directories
            dirs[:] = [d for d in dirs 
                      if not d.startswith('.') 
                      and d not in SKIP_DIRS]

            for file in files:
                file_path = os.path.join(root, file)
                ext = os.path.splitext(file)[1]
                
                file_count += 1
                if file_count % 100 == 0:
                    print(f"  Scanned {file_count} files...")

                # Count file types
                if ext:
                    file_types[ext] = file_types.get(ext, 0) + 1

                # Detect language
                if ext in self.LANGUAGE_EXTENSIONS:
                    technologies.add(self.LANGUAGE_EXTENSIONS[ext])

                # Detect config files
                if file in self.CONFIG_FILES:
                    technologies.add(self.CONFIG_FILES[file])

                # Detect infrastructure
                for infra_file, tech in self.INFRASTRUCTURE_FILES.items():
                    if infra_file in file_path:
                        technologies.add(tech)

                # Count lines for code files
                if ext in self.LANGUAGE_EXTENSIONS:
                    try:
                        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                            lines = sum(1 for _ in f)
                            total_lines += lines
                    except:
                        pass

        metadata.file_types = file_types
        metadata.file_count = sum(file_types.values())
        metadata.total_lines = total_lines
        metadata.technologies = sorted(list(technologies))

        print(f"Scan complete: {metadata.file_count} files, {metadata.total_lines} lines, Technologies: {metadata.technologies}")

        # Extract dependencies
        metadata.dependencies = self._extract_dependencies(local_path)

        # Build structure summary
        metadata.structure = self._build_structure(local_path)

        return metadata

    def _extract_dependencies(self, local_path: str) -> List[str]:
        """Extract dependencies from common dependency files

        Args:
            local_path: Path to repository

        Returns:
            List of dependencies
        """
        dependencies = []

        # Python requirements.txt
        req_file = os.path.join(local_path, 'requirements.txt')
        if os.path.exists(req_file):
            try:
                with open(req_file, 'r') as f:
                    dependencies.extend([
                        line.split('==')[0].split('>=')[0].strip()
                        for line in f if line.strip() and not line.startswith('#')
                    ])
            except:
                pass

        # Node.js package.json
        pkg_file = os.path.join(local_path, 'package.json')
        if os.path.exists(pkg_file):
            try:
                with open(pkg_file, 'r') as f:
                    pkg_data = json.load(f)
                    if 'dependencies' in pkg_data:
                        dependencies.extend(pkg_data['dependencies'].keys())
                    if 'devDependencies' in pkg_data:
                        dependencies.extend(pkg_data['devDependencies'].keys())
            except:
                pass

        return dependencies

    def _build_structure(self, local_path: str) -> Dict:
        """Build directory structure summary

        Args:
            local_path: Path to repository

        Returns:
            Dictionary representing structure
        """
        structure = {
            'root': local_path,
            'directories': [],
            'key_files': []
        }

        # Get top-level directories
        try:
            items = os.listdir(local_path)
            for item in items:
                item_path = os.path.join(local_path, item)
                if os.path.isdir(item_path) and not item.startswith('.'):
                    structure['directories'].append(item)
                elif os.path.isfile(item_path):
                    # Track key configuration files
                    if item in self.CONFIG_FILES or item in ['README.md', 'LICENSE']:
                        structure['key_files'].append(item)
        except:
            pass

        return structure

    def get_code_files(self, local_path: str,
                      extensions: Optional[List[str]] = None) -> List[CodeFile]:
        """Get list of code files from repository

        Args:
            local_path: Path to repository
            extensions: List of file extensions to include (e.g., ['.py', '.java'])

        Returns:
            List of CodeFile objects
        """
        if extensions is None:
            extensions = list(self.LANGUAGE_EXTENSIONS.keys())

        code_files = []

        for root, dirs, files in os.walk(local_path):
            # Skip ignore directories
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in
                      ['node_modules', 'venv', '__pycache__', 'build', 'dist', 'target']]

            for file in files:
                ext = os.path.splitext(file)[1]
                if ext not in extensions:
                    continue

                file_path = os.path.join(root, file)
                relative_path = os.path.relpath(file_path, local_path)

                # Detect if it's a test file
                is_test = (
                    'test' in file.lower() or
                    'test' in relative_path.lower() or
                    file.startswith('test_')
                )

                try:
                    size = os.path.getsize(file_path)
                    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                        lines = sum(1 for _ in f)

                    code_file = CodeFile(
                        path=relative_path,
                        language=self.LANGUAGE_EXTENSIONS.get(ext, 'Unknown'),
                        size_bytes=size,
                        line_count=lines,
                        is_test=is_test
                    )

                    code_files.append(code_file)
                except:
                    pass

        return code_files


def main():
    """Test the repository connector"""
    connector = RepositoryConnector()

    # Example: Connect to local path
    local_path = "c:/Git/IS/infra-structure-ontology"
    print(f"\n=== Scanning Local Repository ===")
    metadata = connector.connect_local_path(local_path, "application")

    print(f"\nRepository Type: {metadata.repo_type}")
    print(f"Local Path: {metadata.local_path}")
    print(f"Branch: {metadata.branch}")
    print(f"File Count: {metadata.file_count}")
    print(f"Total Lines: {metadata.total_lines}")
    print(f"Technologies: {', '.join(metadata.technologies)}")
    print(f"Dependencies: {len(metadata.dependencies)}")
    print(f"Directories: {', '.join(metadata.structure['directories'])}")

    # Get code files
    code_files = connector.get_code_files(local_path, ['.py'])
    print(f"\nPython Files: {len(code_files)}")
    print(f"Test Files: {sum(1 for f in code_files if f.is_test)}")

    # Show some examples
    print("\nExample Files:")
    for cf in code_files[:5]:
        print(f"  - {cf.path} ({cf.language}, {cf.line_count} lines)")


if __name__ == "__main__":
    main()
