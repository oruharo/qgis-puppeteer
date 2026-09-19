"""
Code Analyzer for Python Execution Safety

This module analyzes Python code using AST (Abstract Syntax Tree)
to determine risk level and detect potentially dangerous operations.
"""

import ast
from typing import Literal

RiskLevel = Literal["safe", "medium", "dangerous"]


# Known safe read-only methods (QGIS/PyQGIS)
SAFE_READONLY_METHODS = {
    # QgsProject
    "QgsProject.instance",
    "mapLayers",
    "mapLayersByName",
    "layerTreeRoot",
    # Layer operations (read-only)
    "getFeatures",
    "featureCount",
    "fields",
    "name",
    "source",
    "crs",
    "extent",
    "selectedFeatures",
    "selectedFeatureCount",
    # Geometry operations (read-only)
    "asWkt",
    "asPoint",
    "asPolyline",
    "asPolygon",
    "area",
    "length",
    "centroid",
    "boundingBox",
    "contains",
    "intersects",
    "distance",
    # Attribute operations (read-only)
    "attribute",
    "attributes",
    "fieldNameIndex",
    # iface operations (read-only)
    "mapCanvas",
    "activeLayer",
    "layerTreeView",
}

# Dangerous methods that modify data
DANGEROUS_METHODS = {
    # Layer editing
    "startEditing",
    "commitChanges",
    "rollBack",
    "addFeature",
    "deleteFeature",
    "changeGeometry",
    "changeAttributeValue",
    "deleteAttribute",
    "addAttribute",
    # Layer management
    "addMapLayer",
    "removeMapLayer",
    "removeAllMapLayers",
    # File operations
    "remove",
    "unlink",
    "rmdir",
    "rmtree",
    # Database operations (common patterns)
    "execute",
    "executemany",
    "commit",
    # Settings
    "setValue",
    "setSettings",
}

# Dangerous modules
DANGEROUS_MODULES = {
    "os",
    "subprocess",
    "shutil",
    "pathlib",
}


class CodeAnalyzer:
    """Analyzes Python code for safety and risk level."""

    @staticmethod
    def analyze(code: str) -> dict:
        """Analyze code and return detailed information.

        Args:
            code: Python code to analyze

        Returns:
            Dictionary with analysis results:
            - risk_level: "safe", "medium", or "dangerous"
            - methods_used: List of method calls found
            - dangerous_operations: List of dangerous operations detected
            - modules_imported: List of modules imported
            - has_file_io: Whether code contains file I/O
            - has_db_ops: Whether code contains database operations
        """
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return {
                "risk_level": "dangerous",
                "error": f"Syntax error: {str(e)}",
                "methods_used": [],
                "dangerous_operations": [],
                "modules_imported": [],
                "has_file_io": False,
                "has_db_ops": False,
            }

        visitor = CodeVisitor()
        visitor.visit(tree)

        # Determine risk level
        risk_level = CodeAnalyzer._determine_risk_level(visitor)

        return {
            "risk_level": risk_level,
            "methods_used": list(visitor.methods),
            "dangerous_operations": list(visitor.dangerous_ops),
            "modules_imported": list(visitor.imports),
            "has_file_io": visitor.has_file_io,
            "has_db_ops": visitor.has_db_ops,
        }

    @staticmethod
    def _determine_risk_level(visitor: "CodeVisitor") -> RiskLevel:
        """Determine overall risk level based on visitor results.

        Args:
            visitor: CodeVisitor instance with analysis results

        Returns:
            Risk level: "safe", "medium", or "dangerous"
        """
        # Check for dangerous operations
        if visitor.dangerous_ops:
            return "dangerous"

        # Check for file I/O or database operations
        if visitor.has_file_io or visitor.has_db_ops:
            return "dangerous"

        # Check for dangerous modules
        if any(module in DANGEROUS_MODULES for module in visitor.imports):
            return "dangerous"

        # Check if all methods are known safe
        unknown_methods = visitor.methods - SAFE_READONLY_METHODS
        if unknown_methods:
            return "medium"

        # All checks passed
        return "safe"


class CodeVisitor(ast.NodeVisitor):
    """AST visitor to collect information about code."""

    def __init__(self):
        self.methods: set[str] = set()
        self.dangerous_ops: set[str] = set()
        self.imports: set[str] = set()
        self.has_file_io = False
        self.has_db_ops = False

    def visit_Call(self, node):
        """Visit function/method calls."""
        # Extract method name
        method_name = self._get_call_name(node)
        if method_name:
            self.methods.add(method_name)

            # Check if dangerous
            if any(dangerous in method_name for dangerous in DANGEROUS_METHODS):
                self.dangerous_ops.add(method_name)

            # Check for file I/O
            if any(
                file_op in method_name for file_op in ["open", "write", "read", "remove", "unlink"]
            ):
                self.has_file_io = True

            # Check for database operations
            if any(
                db_op in method_name for db_op in ["execute", "executemany", "commit", "rollback"]
            ):
                self.has_db_ops = True

        self.generic_visit(node)

    def visit_Import(self, node):
        """Visit import statements."""
        for alias in node.names:
            self.imports.add(alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        """Visit from...import statements."""
        if node.module:
            self.imports.add(node.module)
        self.generic_visit(node)

    def _get_call_name(self, node: ast.Call) -> str | None:
        """Extract the full name of a function/method call.

        Args:
            node: AST Call node

        Returns:
            Full method name (e.g., "QgsProject.instance") or None
        """
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute):
            parts = []
            current = node.func
            while isinstance(current, ast.Attribute):
                parts.append(current.attr)
                current = current.value
            if isinstance(current, ast.Name):
                parts.append(current.id)
            return ".".join(reversed(parts))
        return None
