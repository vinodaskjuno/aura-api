"""AURA repo parsers — one parser per repo type, each returns a ParseResult dict."""
from src.parsers.mule_parser import MuleParser
from src.parsers.spring_parser import SpringParser
from src.parsers.python_parser import PythonParser
from src.parsers.ui_parser import UIParser
from src.parsers.node_parser import NodeParser
from src.parsers.terraform_parser import TerraformParser
from src.parsers.cicd_parser import CiCdParser
from src.parsers.config_parser import ConfigParser
from src.parsers.repo_type_detector import detect_repo_type

PARSER_MAP = {
    "mule":       MuleParser,
    "spring":     SpringParser,
    "python":     PythonParser,
    "ui-react":   UIParser,
    "ui-angular": UIParser,
    "node":       NodeParser,
    "terraform":  TerraformParser,
    "cicd":       CiCdParser,
    "config":     ConfigParser,
    # fallbacks — try to extract what we can
    "library":    SpringParser,
    "unknown":    NodeParser,
}

__all__ = [
    "MuleParser", "SpringParser", "PythonParser", "UIParser", "NodeParser",
    "TerraformParser", "CiCdParser", "ConfigParser",
    "detect_repo_type", "PARSER_MAP",
]
