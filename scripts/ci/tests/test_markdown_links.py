import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.ci.markdown_links import (
    PINNED_LINK_EXCEPTIONS,
    check_links,
    parse_document,
)


class MarkdownLinksTests(unittest.TestCase):
    def test_github_heading_anchors_include_unicode_code_and_duplicate_suffixes(self):
        doc = parse_document(
            "# Héllo `world`!\n# Repeat\n# Repeat\n# Repeat-1\n<a id='custom'></a>\n"
        )
        self.assertEqual(
            doc.anchors, {"héllo-world", "repeat", "repeat-1", "repeat-1-1", "custom"}
        )

    def test_inline_reference_images_html_and_nested_parentheses_are_checked(self):
        doc = parse_document(
            "[one](a(b).md#heading) ![two](img.png) [three][ref]\n\n[ref]: b.md\n<a href='c.md#anchor'>link</a>\n<img src='d.png'>\n"
        )
        self.assertEqual(
            {link.target for link in doc.links},
            {"a(b).md#heading", "img.png", "b.md", "c.md#anchor", "d.png"},
        )

    def test_code_examples_do_not_create_links_or_headings(self):
        doc = parse_document(
            "```md\n# fake\n[bad](missing.md)\n```\n\n`[bad](missing.md)`\n\n    [bad](missing.md)\n"
        )
        self.assertFalse(doc.links)
        self.assertFalse(doc.anchors)

    def test_picture_and_media_sources_are_checked(self):
        doc = parse_document(
            '<picture><source srcset="light.png 1x, dark.png 2x">'
            '<img src="fallback.png"></picture>\n'
            '<video src="clip.webm"></video>'
        )
        self.assertEqual(
            {link.target for link in doc.links},
            {"light.png", "dark.png", "fallback.png", "clip.webm"},
        )

    def test_missing_secondary_picture_source_fails_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "existing.png").touch()
            (root / "README.md").write_text(
                '<picture><source srcset="existing.png 1x, missing.png 2x">'
                '<img src="existing.png"></picture>'
            )
            with patch(
                "urllib.request.urlopen",
                side_effect=AssertionError("network forbidden"),
            ):
                findings = check_links(root, ["README.md", "existing.png"])
            self.assertEqual(len(findings), 1)
            self.assertIn("missing.png", findings[0])

    def test_srcset_keeps_url_commas_and_skips_descriptors(self):
        doc = parse_document(
            '<img srcset=" data:image/png;base64,AAAA 1x, '
            'https://invalid.example/image.png 2x, local.png, ">'
        )
        self.assertEqual(
            [link.target for link in doc.links],
            [
                "data:image/png;base64,AAAA",
                "https://invalid.example/image.png",
                "local.png",
            ],
        )

    def test_all_relative_targets_and_anchors_resolve_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "doc space.md").write_text(
                "Heading\n=======\n\n<a name='manual'></a>\n"
            )
            (root / "README.md").write_text(
                "[one](doc%20space.md#heading) [two](#local) [three](doc%20space.md#manual)\n# Local\n[remote](https://invalid.example/no-such-page)\n"
            )
            with patch(
                "urllib.request.urlopen",
                side_effect=AssertionError("network forbidden"),
            ):
                self.assertEqual(check_links(root, ["README.md", "doc space.md"]), [])

    def test_missing_files_untracked_files_and_missing_anchors_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "other.md").write_text("# Existing\n")
            (root / "ignored.md").write_text("# Ignored\n")
            (root / "README.md").write_text(
                "[one](missing.md) [two](other.md#absent) [three](ignored.md)\n"
            )
            findings = check_links(root, ["README.md", "other.md"])
            self.assertEqual(len(findings), 3)
            self.assertIn("missing.md", findings[0])
            self.assertIn("absent", findings[1])
            self.assertIn("ignored.md", findings[2])

    def test_directory_link_resolves_tracked_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docs").mkdir()
            (root / "docs/a.md").write_text("# A")
            (root / "README.md").write_text("[docs](docs/)")
            self.assertEqual(check_links(root, ["README.md", "docs/a.md"]), [])

    def test_directory_fragments_resolve_readme_heading_and_html_anchors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docs").mkdir()
            (root / "docs/readme.md").write_text("# Guide\n<a id='manual'></a>")
            (root / "README.md").write_text(
                "[guide](docs/#guide) [manual](/docs#manual)"
            )
            with patch(
                "urllib.request.urlopen",
                side_effect=AssertionError("network forbidden"),
            ):
                self.assertEqual(check_links(root, ["README.md", "docs/readme.md"]), [])

    def test_directory_fragment_with_absent_readme_heading_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docs").mkdir()
            (root / "docs/README.md").write_text("# Guide")
            (root / "README.md").write_text("[absent](docs/#absent)")
            findings = check_links(root, ["README.md", "docs/README.md"])
            self.assertEqual(len(findings), 1)
            self.assertIn("docs/#absent", findings[0])

    def test_directory_fragment_cannot_resolve_an_untracked_readme(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docs").mkdir()
            (root / "docs/a.md").write_text("# Tracked")
            (root / "docs/README.md").write_text("# Untracked")
            (root / "README.md").write_text("[ignored](docs/#untracked)")
            findings = check_links(root, ["README.md", "docs/a.md"])
            self.assertEqual(len(findings), 1)
            self.assertIn("docs/#untracked", findings[0])

    def test_only_the_three_approved_vendor_links_are_excepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tracked = []
            for name, line, target in PINNED_LINK_EXCEPTIONS:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                source = path.read_text() if path.exists() else ""
                source += "\n" * (line - source.count("\n") - 1)
                path.write_text(source + f"[reference]({target})\n")
                if name not in tracked:
                    tracked.append(name)
            self.assertEqual(len(PINNED_LINK_EXCEPTIONS), 3)
            self.assertEqual(check_links(root, tracked), [])
            (root / "README.md").write_text(
                "[bad](references/id_mapping_documentation.md)"
            )
            path.write_text(path.read_text() + "\n[bad](other-missing.md)\n")
            findings = check_links(root, [*tracked, "README.md"])
            self.assertEqual(len(findings), 2)

    def test_exception_does_not_cover_shifted_or_duplicate_vendor_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            name, line, target = next(iter(PINNED_LINK_EXCEPTIONS))
            path = root / name
            path.parent.mkdir(parents=True)
            link = f"[reference]({target})"
            path.write_text("\n" * (line - 1) + link + " " + link)
            self.assertEqual(len(check_links(root, [name])), 1)
            path.write_text("\n" * line + link)
            self.assertEqual(len(check_links(root, [name])), 1)


if __name__ == "__main__":
    unittest.main()
