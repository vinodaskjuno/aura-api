"""
Code Analyzer Service for SDLC Ontology System
Performs AST analysis to understand code structure
"""
import ast
import os
from pathlib import Path
from typing import Dict, List, Optional, Set
from dataclasses import dataclass, field
import re


@dataclass
class ClassInfo:
    """Information about a class"""
    name: str
    file_path: str
    line_start: int
    line_end: int
    methods: List[str] = field(default_factory=list)
    base_classes: List[str] = field(default_factory=list)
    docstring: Optional[str] = None
    complexity: int = 0
    is_test: bool = False


@dataclass
class FunctionInfo:
    """Information about a function"""
    name: str
    file_path: str
    line_start: int
    line_end: int
    parameters: List[str] = field(default_factory=list)
    returns: Optional[str] = None
    docstring: Optional[str] = None
    complexity: int = 0
    is_test: bool = False


@dataclass
class ModuleInfo:
    """Information about a module"""
    name: str
    file_path: str
    imports: List[str] = field(default_factory=list)
    classes: List[ClassInfo] = field(default_factory=list)
    functions: List[FunctionInfo] = field(default_factory=list)
    line_count: int = 0
    docstring: Optional[str] = None


@dataclass
class TestInfo:
    """Information about a test"""
    name: str
    test_class: Optional[str] = None
    file_path: str = ""
    line_number: int = 0
    tests_class: Optional[str] = None  # Class being tested
    status: str = "pending"  # pending, running, passed, failed


@dataclass
class AnalysisResult:
    """Result of code analysis"""
    modules: List[ModuleInfo] = field(default_factory=list)
    classes: List[ClassInfo] = field(default_factory=list)
    functions: List[FunctionInfo] = field(default_factory=list)
    tests: List[TestInfo] = field(default_factory=list)
    dependencies: Dict[str, List[str]] = field(default_factory=dict)
    total_lines: int = 0
    total_classes: int = 0
    total_functions: int = 0
    total_tests: int = 0
    test_coverage_percent: float = 0.0


class PythonCodeAnalyzer:
    """Analyzer for Python code using AST"""

    def __init__(self):
        self.test_patterns = [
            r'^test_',
            r'_test$',
            r'Test',
        ]

    def analyze_file(self, file_path: str) -> Optional[ModuleInfo]:
        """Analyze a single Python file

        Args:
            file_path: Path to Python file

        Returns:
            ModuleInfo with extracted information
        """
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                source = f.read()

            tree = ast.parse(source, filename=file_path)

            module_name = os.path.splitext(os.path.basename(file_path))[0]
            module_info = ModuleInfo(
                name=module_name,
                file_path=file_path,
                line_count=len(source.splitlines())
            )

            # Extract module docstring
            if (isinstance(tree, ast.Module) and tree.body and
                isinstance(tree.body[0], ast.Expr) and
                isinstance(tree.body[0].value, ast.Constant)):
                module_info.docstring = tree.body[0].value.value

            # Visit AST nodes
            for node in ast.walk(tree):
                # Extract imports
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    module_info.imports.extend(self._extract_imports(node))

                # Extract classes
                elif isinstance(node, ast.ClassDef):
                    class_info = self._extract_class_info(node, file_path)
                    module_info.classes.append(class_info)

                # Extract top-level functions
                elif isinstance(node, ast.FunctionDef):
                    # Check if it's a top-level function (not a method)
                    if not any(isinstance(parent, ast.ClassDef)
                              for parent in ast.walk(tree)
                              if node in getattr(parent, 'body', [])):
                        func_info = self._extract_function_info(node, file_path)
                        module_info.functions.append(func_info)

            return module_info

        except Exception as e:
            print(f"Error analyzing {file_path}: {e}")
            return None

    def _extract_imports(self, node) -> List[str]:
        """Extract import statements"""
        imports = []

        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ''
            for alias in node.names:
                imports.append(f"{module}.{alias.name}" if module else alias.name)

        return imports

    def _extract_class_info(self, node: ast.ClassDef, file_path: str) -> ClassInfo:
        """Extract class information"""
        class_info = ClassInfo(
            name=node.name,
            file_path=file_path,
            line_start=node.lineno,
            line_end=node.end_lineno or node.lineno,
            is_test=self._is_test_name(node.name)
        )

        # Extract docstring
        if (node.body and isinstance(node.body[0], ast.Expr) and
            isinstance(node.body[0].value, ast.Constant)):
            class_info.docstring = node.body[0].value.value

        # Extract base classes
        for base in node.bases:
            if isinstance(base, ast.Name):
                class_info.base_classes.append(base.id)
            elif isinstance(base, ast.Attribute):
                class_info.base_classes.append(base.attr)

        # Extract methods
        for item in node.body:
            if isinstance(item, ast.FunctionDef):
                class_info.methods.append(item.name)

        # Calculate complexity (simple: number of methods + conditionals)
        class_info.complexity = len(class_info.methods)

        return class_info

    def _extract_function_info(self, node: ast.FunctionDef, file_path: str) -> FunctionInfo:
        """Extract function information"""
        func_info = FunctionInfo(
            name=node.name,
            file_path=file_path,
            line_start=node.lineno,
            line_end=node.end_lineno or node.lineno,
            is_test=self._is_test_name(node.name)
        )

        # Extract docstring
        if (node.body and isinstance(node.body[0], ast.Expr) and
            isinstance(node.body[0].value, ast.Constant)):
            func_info.docstring = node.body[0].value.value

        # Extract parameters
        for arg in node.args.args:
            func_info.parameters.append(arg.arg)

        # Extract return type if annotated
        if node.returns:
            if isinstance(node.returns, ast.Name):
                func_info.returns = node.returns.id
            elif isinstance(node.returns, ast.Constant):
                func_info.returns = str(node.returns.value)

        # Calculate complexity (count conditionals and loops)
        complexity = 0
        for child in ast.walk(node):
            if isinstance(child, (ast.If, ast.For, ast.While, ast.ExceptHandler)):
                complexity += 1
        func_info.complexity = complexity

        return func_info

    def _is_test_name(self, name: str) -> bool:
        """Check if name matches test patterns"""
        return any(re.search(pattern, name) for pattern in self.test_patterns)

    def analyze_directory(self, directory: str, max_files: int = 500) -> AnalysisResult:
        """Analyze all Python files in a directory

        Args:
            directory: Path to directory
            max_files: Maximum number of files to analyze (prevents hanging on large repos)

        Returns:
            AnalysisResult with aggregated information
        """
        result = AnalysisResult()

        # Directories to skip (performance optimization)
        SKIP_DIRS = {
            '.venv', 'venv', 'env', 'ENV',
            '__pycache__', '.pytest_cache',
            'node_modules', '.git', '.github',
            'build', 'dist', '.tox', '.eggs',
            'htmlcov', '.mypy_cache', '.coverage',
            'site-packages', 'lib', 'lib64'
        }

        # Find all Python files
        python_files = []
        file_count = 0
        
        for root, dirs, files in os.walk(directory):
            # Skip hidden and common ignore directories
            dirs[:] = [d for d in dirs 
                      if not d.startswith('.') 
                      and d not in SKIP_DIRS]

            for file in files:
                if file.endswith('.py') and not file.startswith('.'):
                    file_path = os.path.join(root, file)
                    python_files.append(file_path)
                    file_count += 1
                    
                    # Limit file count to prevent hanging
                    if file_count >= max_files:
                        print(f"Warning: Reached max file limit ({max_files}), stopping scan")
                        break
            
            if file_count >= max_files:
                break

        print(f"Analyzing {len(python_files)} Python files in {directory}")
        
        # Analyze each file
        for idx, file_path in enumerate(python_files):
            if idx % 10 == 0 and idx > 0:
                print(f"  Progress: {idx}/{len(python_files)} files analyzed...")
            
            module_info = self.analyze_file(file_path)
            if module_info:
                result.modules.append(module_info)
                result.classes.extend(module_info.classes)
                result.functions.extend(module_info.functions)
                result.total_lines += module_info.line_count
        
        print(f"Analysis complete: {len(result.classes)} classes, {len(result.functions)} functions")

        # Extract test information
        result.tests = self._extract_tests(result)

        # Calculate statistics
        result.total_classes = len(result.classes)
        result.total_functions = len(result.functions)
        result.total_tests = len(result.tests)

        # Calculate test coverage (simple: % of classes with tests)
        if result.total_classes > 0:
            tested_classes = len(set(t.tests_class for t in result.tests if t.tests_class))
            result.test_coverage_percent = (tested_classes / result.total_classes) * 100

        # Build dependency graph
        result.dependencies = self._build_dependency_graph(result)

        return result

    def _extract_tests(self, result: AnalysisResult) -> List[TestInfo]:
        """Extract test information from analysis result"""
        tests = []

        # Find test classes and functions
        for cls in result.classes:
            if cls.is_test:
                for method in cls.methods:
                    if self._is_test_name(method):
                        test = TestInfo(
                            name=f"{cls.name}.{method}",
                            test_class=cls.name,
                            file_path=cls.file_path,
                            line_number=cls.line_start
                        )

                        # Try to infer what class is being tested
                        test.tests_class = self._infer_tested_class(cls.name)
                        tests.append(test)

        # Find standalone test functions
        for func in result.functions:
            if func.is_test:
                test = TestInfo(
                    name=func.name,
                    file_path=func.file_path,
                    line_number=func.line_start
                )
                tests.append(test)

        return tests

    def _infer_tested_class(self, test_class_name: str) -> Optional[str]:
        """Infer the class being tested from test class name

        Examples:
            TestUserService -> UserService
            TestClaimValidator -> ClaimValidator
            UserServiceTest -> UserService
        """
        # Remove 'Test' prefix
        if test_class_name.startswith('Test'):
            return test_class_name[4:]

        # Remove 'Test' suffix
        if test_class_name.endswith('Test'):
            return test_class_name[:-4]

        return None

    def _build_dependency_graph(self, result: AnalysisResult) -> Dict[str, List[str]]:
        """Build dependency graph from imports"""
        dependencies = {}

        for module in result.modules:
            # Filter to local imports (those that exist in our modules)
            module_names = {m.name for m in result.modules}
            local_imports = [
                imp for imp in module.imports
                if imp.split('.')[0] in module_names
            ]

            if local_imports:
                dependencies[module.name] = local_imports

        return dependencies

    def get_test_execution_plan(self, result: AnalysisResult) -> Dict:
        """Generate a test execution plan

        Args:
            result: Analysis result

        Returns:
            Dictionary with test execution plan
        """
        # Group tests by file and class
        tests_by_file = {}
        tests_by_class = {}

        for test in result.tests:
            # Group by file
            if test.file_path not in tests_by_file:
                tests_by_file[test.file_path] = []
            tests_by_file[test.file_path].append(test)

            # Group by tested class
            if test.tests_class:
                if test.tests_class not in tests_by_class:
                    tests_by_class[test.tests_class] = []
                tests_by_class[test.tests_class].append(test)

        return {
            'total_tests': len(result.tests),
            'total_files': len(tests_by_file),
            'tests_by_file': {
                file: [t.name for t in tests]
                for file, tests in tests_by_file.items()
            },
            'tests_by_class': {
                cls: [t.name for t in tests]
                for cls, tests in tests_by_class.items()
            },
            'untested_classes': [
                cls.name for cls in result.classes
                if not cls.is_test and cls.name not in tests_by_class
            ]
        }


if __name__ == "__main__":
    pass
