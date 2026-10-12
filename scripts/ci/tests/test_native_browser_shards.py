import copy
import unittest

from scripts.ci.native_browser_shards import partition_files


def collection(files, project="webkit"):
    return {
        "errors": [],
        "suites": [
            {
                "specs": [
                    {
                        "file": name,
                        "id": f"{name}-{index}",
                        "tests": [{"projectName": project}],
                    }
                    for name, count in files.items()
                    for index in range(count)
                ]
            }
        ],
    }


class NativeBrowserShardsTests(unittest.TestCase):
    def setUp(self):
        self.files = {
            "announcements.spec.ts": 2,
            "keyboard.spec.ts": 2,
            "progress-keyboard.spec.ts": 4,
            "semantics.spec.ts": 2,
        }

    def test_measured_heavy_file_balances_against_three_shorter_files(self):
        bins = partition_files(collection(self.files), "webkit")
        self.assertEqual(bins[0], ["progress-keyboard.spec.ts"])
        self.assertEqual(set(bins[1]), set(self.files) - set(bins[0]))
        self.assertEqual(
            [sum(self.files[name] for name in group) for group in bins], [4, 6]
        )

    def test_unknown_future_and_nested_files_are_included_exactly_once(self):
        self.files["nested/new-tagged-guard.spec.js"] = 3
        bins = partition_files(collection(self.files), "webkit")
        flattened = [name for group in bins for name in group]
        self.assertEqual(set(flattened), set(self.files))
        self.assertEqual(len(flattened), len(self.files))

    def test_future_cases_in_existing_files_change_weight_without_disappearing(self):
        self.files["semantics.spec.ts"] = 12
        bins = partition_files(collection(self.files), "webkit")
        self.assertEqual(set(name for group in bins for name in group), set(self.files))
        self.assertNotEqual(
            bins,
            partition_files(
                collection({**self.files, "semantics.spec.ts": 2}), "webkit"
            ),
        )

    def test_collection_order_never_changes_partition(self):
        report = collection(self.files)
        reversed_report = copy.deepcopy(report)
        reversed_report["suites"][0]["specs"].reverse()
        self.assertEqual(
            partition_files(report, "webkit"),
            partition_files(reversed_report, "webkit"),
        )

    def test_three_nonempty_bins_keep_every_selected_file_and_case(self):
        bins = partition_files(collection(self.files), "webkit", shards=3)
        self.assertEqual(len(bins), 3)
        self.assertTrue(all(bins))
        flattened = [name for group in bins for name in group]
        self.assertCountEqual(flattened, self.files)
        self.assertEqual(
            sum(self.files[name] for name in flattened), sum(self.files.values())
        )

    def test_three_bins_retain_unknown_files_and_ignore_other_projects(self):
        self.files["nested/future.spec.ts"] = 3
        report = collection(self.files)
        report["suites"] += collection({"other-project.spec.ts": 4}, "firefox")[
            "suites"
        ]
        bins = partition_files(report, "webkit", shards=3)
        self.assertCountEqual([name for group in bins for name in group], self.files)

    def test_invalid_counts_and_empty_third_bin_fail(self):
        for count in (0, 1, 4, "3"):
            with self.subTest(count=count), self.assertRaises(ValueError):
                partition_files(collection(self.files), "webkit", shards=count)
        with self.assertRaises(ValueError):
            partition_files(
                collection({"one.spec.ts": 2, "two.spec.ts": 2}), "webkit", shards=3
            )

    def test_duplicate_ids_and_unsafe_paths_fail(self):
        report = collection(self.files)
        report["suites"][0]["specs"].append(report["suites"][0]["specs"][0])
        with self.assertRaises(ValueError):
            partition_files(report, "webkit")
        for name in (
            "../outside.spec.ts",
            "/tmp/outside.spec.ts",
            "nested/../../outside.spec.ts",
            "bad\\path.spec.ts",
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                partition_files(collection({name: 2, "safe.spec.ts": 2}), "webkit")

    def test_errors_empty_or_wrong_project_and_empty_shard_fail(self):
        for report in (
            {"errors": ["collection failed"], "suites": []},
            collection({}),
            collection(self.files, "firefox"),
            collection({"one-file.spec.ts": 10}),
            {"suites": [None]},
        ):
            with self.subTest(report=report), self.assertRaises(ValueError):
                partition_files(report, "webkit")


if __name__ == "__main__":
    unittest.main()
