"""Guard architectural boundaries and supported compatibility imports."""
import ast
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1] / 'walletwatcher'


def imports(path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                package = path.relative_to(ROOT.parent).with_suffix('').parts[:-1]
                base = '.'.join(package[:len(package) - node.level + 1])
                module = base + ('.' + node.module if node.module else '')
            else:
                module = node.module
            if module:
                yield module
                yield from (module + '.' + alias.name for alias in node.names if alias.name != '*')


class ArchitectureTests(unittest.TestCase):
    def test_domain_does_not_depend_on_adapters_or_external_packages(self):
        for path in (ROOT / 'domain').glob('*.py'):
            for module in imports(path):
                with self.subTest(file=path.name, dependency=module):
                    if module.startswith('walletwatcher.'):
                        self.assertTrue(module == 'walletwatcher.domain' or module.startswith('walletwatcher.domain.'))
                    else:
                        self.assertIn(module.split('.')[0], sys.stdlib_module_names)
                        self.assertNotIn(module.split('.')[0], ('socket', 'http', 'urllib', 'sqlite3'))

    def test_api_adapters_do_not_depend_on_telegram_presentation(self):
        for path in (ROOT / 'hyperliquid').glob('*.py'):
            for module in imports(path):
                with self.subTest(file=path.name, dependency=module):
                    self.assertFalse(module.startswith('walletwatcher.presentation'))
                    self.assertNotEqual(module.split('.')[0], 'telebot')

    def test_trading_policy_and_gateway_have_no_concrete_io_dependencies(self):
        for name in ('policy.py', 'gateway.py', 'ports.py'):
            for module in imports(ROOT / 'trading' / name):
                with self.subTest(file=name, dependency=module):
                    if module.startswith('walletwatcher.'):
                        self.assertTrue(module.startswith('walletwatcher.domain.')
                                        or module == 'walletwatcher.trading.policy'
                                        or module.startswith('walletwatcher.trading.policy.'))
                    else:
                        self.assertIn(module.split('.')[0], sys.stdlib_module_names)

    def test_services_do_not_depend_on_application_composition(self):
        paths = [*(ROOT / 'services').glob('*.py'),
                 ROOT / 'presentation' / 'telegram.py', ROOT / 'trading' / 'worker.py']
        for path in paths:
            for module in imports(path):
                with self.subTest(file=path.name, dependency=module):
                    self.assertFalse(module == 'walletwatcher.app' or module.startswith('walletwatcher.app.'))

    def test_autoclose_worker_does_not_construct_concrete_adapters(self):
        for module in imports(ROOT / 'trading' / 'worker.py'):
            with self.subTest(dependency=module):
                self.assertFalse(module.startswith(('walletwatcher.trading.api',
                                                    'walletwatcher.hyperliquid.client',
                                                    'walletwatcher.storage', 'walletwatcher.services',
                                                    'walletwatcher.presentation')))

    def test_project_import_graph_has_no_cycles(self):
        modules = {'.'.join(path.relative_to(ROOT.parent).with_suffix('').parts): path
                   for path in ROOT.rglob('*.py')}
        graph = {name: {candidate for dependency in imports(path)
                        for candidate in (dependency, dependency + '.__init__') if candidate in modules}
                 for name, path in modules.items()}
        visited, stack = set(), []
        def visit(name):
            self.assertNotIn(name, stack, 'Import cycle: ' + ' -> '.join([*stack, name]))
            if name in visited:
                return
            stack.append(name)
            for dependency in sorted(graph[name]):
                visit(dependency)
            stack.pop()
            visited.add(name)
        for name in sorted(graph):
            visit(name)

    def test_legacy_imports_reference_the_same_models_and_parsers(self):
        from walletwatcher.domain.credentials import Credentials
        from walletwatcher.domain.portfolio import PersonalPosition, Portfolio
        from walletwatcher.domain.validation import label, number
        from walletwatcher.hyperliquid import parsing
        from walletwatcher.presentation import formatting, portfolio
        from walletwatcher.storage import credentials

        self.assertIs(credentials.Credentials, Credentials)
        self.assertIs(portfolio.Portfolio, Portfolio)
        self.assertIs(portfolio.PersonalPosition, PersonalPosition)
        self.assertIs(portfolio.parse_positions, parsing.parse_positions)
        self.assertIs(formatting.format_position, parsing.format_position)
        self.assertIs(formatting.format_trade, parsing.format_trade)
        self.assertIs(formatting.number, number)
        self.assertIs(formatting.label, label)
