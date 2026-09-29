import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_grading.FileResolver import FileResolver
from ai_grading.models import GradingInputError, TargetKind


class FileResolverTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.resolver = FileResolver()

    def write(self, path, content="source body"):
        destination = self.root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
        return destination

    def test_java_class_and_method_read_the_whole_source(self):
        self.write("src/edu/pitt/cs/Statistics.java", "class Statistics { /* π */ }")
        for identifier, kind in (("edu.pitt.cs.Statistics", TargetKind.CLASS),
                                 ("edu.pitt.cs.Statistics#median", TargetKind.METHOD)):
            with self.subTest(kind=kind):
                result = self.resolver.resolve(identifier, kind, "JAVA", self.root)
                self.assertEqual(result.source_file.path, "src/edu/pitt/cs/Statistics.java")
                self.assertEqual(result.source_file.content, "class Statistics { /* π */ }")

    def test_java_nested_class_notation_resolves_outer_file(self):
        self.write("Outer.java")
        for identifier in ("pkg.Outer$Inner", "pkg.Outer.Inner", "pkg.Outer.Inner$Nested"):
            with self.subTest(identifier=identifier):
                result = self.resolver.resolve(identifier, "CLASS", "java", self.root)
                self.assertEqual(result.source_file.path, "Outer.java")
                method = self.resolver.resolve(identifier + "#calculate", "METHOD", "java", self.root)
                self.assertEqual(method.source_file.path, "Outer.java")

    def test_java_nested_class_uses_outer_file_even_when_inner_file_exists(self):
        self.write("Outer.java")
        self.write("Inner.java")
        result = self.resolver.resolve("pkg.Outer.Inner", "CLASS", "java", self.root)
        self.assertEqual(result.source_file.path, "Outer.java")
        self.assertEqual(result.other_file_paths, ("Inner.java",))

    def test_java_missing_outer_class_never_falls_back_to_inner_file(self):
        self.write("Inner.java")
        with self.assertRaisesRegex(GradingInputError, "No source file.*pkg.Outer.Inner"):
            self.resolver.resolve("pkg.Outer.Inner", "CLASS", "java", self.root)

    def test_java_rejects_ambiguous_nonconventional_names(self):
        self.write("source/statistics.java")
        with self.assertRaisesRegex(GradingInputError, "use FILE for other naming conventions"):
            self.resolver.resolve("pkg.statistics#median", "METHOD", "java", self.root)
        result = self.resolver.resolve("pkg.statistics$Inner", "CLASS", "java", self.root)
        self.assertEqual(result.source_file.path, "source/statistics.java")

    def test_java_backup_copy_is_ambiguous(self):
        self.write("src/Statistics.java")
        self.write("backup/Statistics.java")
        with self.assertRaisesRegex(GradingInputError, "Multiple source files.*backup/Statistics.java, src/Statistics.java"):
            self.resolver.resolve("pkg.Statistics", "CLASS", "java", self.root)

    def test_python_explicit_path_and_qualname(self):
        self.write("src/report.py", "class Report:\n    pass\n")
        for kind, qualname in (("CLASS", "Report"), ("METHOD", "Report.average")):
            with self.subTest(kind=kind):
                result = self.resolver.resolve(f"src/report.py:{qualname}", kind, "python", self.root)
                self.assertEqual(result.source_file.path, "src/report.py")

    def test_python_rejects_undeclared_dotted_module_conventions(self):
        self.write("report.py")
        for identifier in ("report.average", "report#average", "report.py:", "*.py:average"):
            with self.subTest(identifier=identifier), self.assertRaisesRegex(GradingInputError, "relative/module.py:qualname"):
                self.resolver.resolve(identifier, "METHOD", "python", self.root)

    def test_file_glob_matches_zero_or_more_directories(self):
        self.write("src/report.py")
        for pattern in ("src/report.py", "src/**/*.py", "**/*.py"):
            with self.subTest(pattern=pattern):
                result = self.resolver.resolve(pattern, "FILE", "any-language", self.root)
                self.assertEqual(result.source_file.path, "src/report.py")
        self.write("src/nested/extra.py")
        result = self.resolver.resolve("src/*.py", "FILE", "python", self.root)
        self.assertEqual(result.source_file.path, "src/report.py")
        with self.assertRaisesRegex(GradingInputError, "Multiple source files"):
            self.resolver.resolve("src/**/*.py", "FILE", "python", self.root)

    def test_recursive_glob_also_matches_root_file(self):
        self.write("report.py")
        self.assertEqual(self.resolver.resolve("**/*.py", "FILE", "python", self.root).source_file.path,
                         "report.py")

    def test_other_files_are_sorted_names_only_and_never_read(self):
        self.write("report.py", "print('main')")
        self.write("z/helper.py", "DO NOT READ THIS")
        (self.root / "binary.dat").write_bytes(b"\xff\xfe")
        self.write("a.txt", "notes")
        original_read_text = Path.read_text
        reads = []

        def track_read(path, *args, **kwargs):
            reads.append(path.relative_to(self.root.resolve()).as_posix())
            return original_read_text(path, *args, **kwargs)

        with patch.object(Path, "read_text", track_read):
            result = self.resolver.resolve("report.py", "FILE", "python", self.root)
        self.assertEqual(reads, ["report.py"])
        self.assertEqual(result.source_file.content, "print('main')")
        self.assertEqual(result.other_file_paths, ("a.txt", "binary.dat", "z/helper.py"))

    def test_zero_matches_is_a_configuration_failure(self):
        with self.assertRaisesRegex(GradingInputError, "No source file.*Missing.*configured identifier"):
            self.resolver.resolve("pkg.Missing", "CLASS", "java", self.root)

    def test_rejects_paths_outside_submission(self):
        for identifier in ("../secret.py", "src/../../secret.py", "/tmp/secret.py",
                           "C:/secret.py", "C:\\secret.py", "bad\x00.py"):
            with self.subTest(identifier=identifier), self.assertRaisesRegex(GradingInputError, "relative POSIX"):
                self.resolver.resolve(identifier, "FILE", "python", self.root)
        with self.assertRaisesRegex(GradingInputError, "relative POSIX"):
            self.resolver.resolve("../outside.py:average", "METHOD", "python", self.root)

    def test_rejects_source_and_directory_symlink_escape(self):
        with tempfile.TemporaryDirectory() as outside:
            destination = Path(outside) / "outside.py"
            destination.write_text("must not be read", encoding="utf-8")
            for target in (destination, Path(outside)):
                link = self.root / "escape"
                link.symlink_to(target)
                try:
                    with self.assertRaisesRegex(GradingInputError, "Symbolic link.*escapes"):
                        self.resolver.resolve("**/*", "FILE", "python", self.root)
                finally:
                    link.unlink()

    def test_internal_source_symlink_is_rejected_and_directory_link_is_not_traversed(self):
        self.write("src/report.py")
        (self.root / "linked.py").symlink_to(self.root / "src/report.py")
        with self.assertRaisesRegex(GradingInputError, "symbolic link"):
            self.resolver.resolve("linked.py", "FILE", "python", self.root)
        (self.root / "linked.py").unlink()
        (self.root / "loop").symlink_to(self.root)
        result = self.resolver.resolve("**/*.py", "FILE", "python", self.root)
        self.assertEqual(result.source_file.path, "src/report.py")
        self.assertEqual(result.other_file_paths, ())

    def test_invalid_utf8_and_unreadable_source_fail_clearly(self):
        source = self.root / "broken.py"
        source.write_bytes(b"\xff\xfe")
        with self.assertRaisesRegex(GradingInputError, "broken.py.*UTF-8"):
            self.resolver.resolve("broken.py", "FILE", "python", self.root)
        self.write("broken.py")
        with patch.object(Path, "read_text", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(GradingInputError, "Cannot read source file.*broken.py.*permissions"):
                self.resolver.resolve("broken.py", "FILE", "python", self.root)

    def test_missing_directory_unsupported_language_and_invalid_java_identifiers(self):
        with self.assertRaisesRegex(GradingInputError, "Cannot inspect the submission directory"):
            self.resolver.resolve("main.py", "FILE", "python", self.root / "missing")
        with self.assertRaisesRegex(GradingInputError, "supported language"):
            self.resolver.resolve("Thing", "CLASS", "ruby", self.root)
        for identifier, kind in (("Thing", "METHOD"), ("Thing#", "METHOD"),
                                 ("Thing#method", "CLASS"), ("bad/name", "CLASS")):
            with self.subTest(identifier=identifier), self.assertRaises(GradingInputError):
                self.resolver.resolve(identifier, kind, "java", self.root)


if __name__ == "__main__":
    unittest.main()
