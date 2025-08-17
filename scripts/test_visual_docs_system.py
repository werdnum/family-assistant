#!/usr/bin/env python3
"""Test script to verify the visual documentation system works properly."""

import os
import subprocess
import sys
from pathlib import Path


def test_marker_collection() -> bool:
    """Test that the visual documentation tests are properly marked."""
    print("🔍 Testing pytest marker collection...")

    # Test with GENERATE_VISUAL_DOCS=1
    env = os.environ.copy()
    env["GENERATE_VISUAL_DOCS"] = "1"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-m",
            "visual_documentation",
            "--collect-only",
            "tests/functional/web/test_visual_documentation.py",
        ],
        env=env,
        capture_output=True,
        text=True,
    )

    if result.returncode == 0:
        lines = result.stdout.split("\n")
        collected_line = [line for line in lines if "tests collected" in line]
        if collected_line:
            print(f"✅ Collection successful: {collected_line[0]}")
        else:
            print("✅ Collection successful (no summary line found)")
    else:
        print(f"❌ Collection failed: {result.stderr}")
        return False

    # Test without env var (should still collect but would skip on run)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-m",
            "visual_documentation",
            "--collect-only",
            "tests/functional/web/test_visual_documentation.py",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode == 0:
        print("✅ Collection without env var also successful")
    else:
        print(f"❌ Collection without env var failed: {result.stderr}")
        return False

    return True


def test_dashboard_generator() -> bool:
    """Test the dashboard generator script."""
    print("\\n📊 Testing dashboard generator...")

    # Create some mock data
    test_dir = Path("test-visual-docs")
    test_dir.mkdir(exist_ok=True)

    # Create mock directory structure
    (test_dir / "mobile-light" / "chat").mkdir(parents=True, exist_ok=True)
    (test_dir / "desktop-light" / "chat").mkdir(parents=True, exist_ok=True)

    # Create mock files
    (test_dir / "mobile-light" / "chat" / "01-test.png").touch()
    (test_dir / "desktop-light" / "chat" / "01-test.png").touch()

    # Test dashboard generation
    result = subprocess.run(
        [
            sys.executable,
            "scripts/generate_visual_docs_dashboard.py",
            "--base-dir",
            str(test_dir),
            "--output",
            str(test_dir / "test-dashboard.html"),
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode == 0:
        print("✅ Dashboard generation successful")

        # Check files were created
        if (test_dir / "test-dashboard.html").exists():
            print("✅ HTML dashboard created")
        else:
            print("❌ HTML dashboard not found")
            return False

        if (test_dir / "metadata.json").exists():
            print("✅ Metadata JSON created")
        else:
            print("❌ Metadata JSON not found")
            return False
    else:
        print(f"❌ Dashboard generation failed: {result.stderr}")
        return False

    # Cleanup
    import shutil

    shutil.rmtree(test_dir)

    return True


def test_workflow_yaml() -> bool:
    """Test that the workflow YAML is valid."""
    print("\\n🔄 Testing GitHub workflow...")

    workflow_path = Path(".github/workflows/visual-documentation.yml")
    if not workflow_path.exists():
        print("❌ Workflow file not found")
        return False

    print("✅ Workflow file exists")

    # Basic YAML syntax check
    try:
        import yaml

        with open(workflow_path) as f:
            yaml.safe_load(f)
        print("✅ Workflow YAML syntax is valid")
    except ImportError:
        print("⚠️ PyYAML not available, skipping YAML validation")
    except yaml.YAMLError as e:
        print(f"❌ Workflow YAML syntax error: {e}")
        return False

    return True


def main() -> int:
    """Run all tests."""
    print("🚀 Testing Visual Documentation System\\n")

    tests = [
        ("Pytest Marker Collection", test_marker_collection),
        ("Dashboard Generator", test_dashboard_generator),
        ("GitHub Workflow", test_workflow_yaml),
    ]

    results = []
    for name, test_func in tests:
        print(f"Running {name}...")
        try:
            success = test_func()
            results.append((name, success))
        except Exception as e:
            print(f"❌ {name} failed with exception: {e}")
            results.append((name, False))

    print("\\n📋 Summary:")
    all_passed = True
    for name, success in results:
        status = "✅ PASS" if success else "❌ FAIL"
        print(f"  {status}: {name}")
        if not success:
            all_passed = False

    if all_passed:
        print(
            "\\n🎉 All tests passed! The visual documentation system is ready to use."
        )
        print("\\nNext steps:")
        print("1. Commit and push the feature branch")
        print("2. Test the GitHub workflow manually")
        print(
            "3. Generate actual screenshots with: GENERATE_VISUAL_DOCS=1 pytest -m visual_documentation"
        )
    else:
        print("\\n💥 Some tests failed. Please fix the issues above.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
