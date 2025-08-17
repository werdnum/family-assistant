#!/usr/bin/env python3
"""Generate HTML dashboard for visual documentation screenshots.

This script scans the visual-docs directory and generates an HTML dashboard
for easy review of all captured screenshots organized by flow and viewport.

Usage:
    python scripts/generate_visual_docs_dashboard.py
    python scripts/generate_visual_docs_dashboard.py --output custom-dashboard.html
"""

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any


def scan_screenshots(base_dir: Path) -> dict[str, Any]:
    """Scan the visual-docs directory and organize screenshots by flow and viewport."""
    screenshots = {}

    if not base_dir.exists():
        return screenshots

    # Look for viewport-theme directories (e.g., mobile-light, desktop-dark)
    for viewport_dir in base_dir.iterdir():
        if not viewport_dir.is_dir() or viewport_dir.name.startswith("."):
            continue

        # Parse viewport and theme from directory name
        try:
            viewport, theme = viewport_dir.name.split("-", 1)
        except ValueError:
            continue

        # Look for flow directories within each viewport
        for flow_dir in viewport_dir.iterdir():
            if not flow_dir.is_dir():
                continue

            flow_name = flow_dir.name
            if flow_name not in screenshots:
                screenshots[flow_name] = {}

            viewport_key = f"{viewport}-{theme}"
            if viewport_key not in screenshots[flow_name]:
                screenshots[flow_name][viewport_key] = []

            # Collect all PNG files in the flow directory
            for screenshot_file in sorted(flow_dir.glob("*.png")):
                screenshots[flow_name][viewport_key].append({
                    "filename": screenshot_file.name,
                    "path": str(screenshot_file.relative_to(base_dir)),
                    "size": screenshot_file.stat().st_size,
                    "modified": datetime.fromtimestamp(screenshot_file.stat().st_mtime),
                })

    return screenshots


def generate_dashboard_html(screenshots: dict[str, Any], output_path: Path) -> None:
    """Generate the HTML dashboard."""

    # Get all unique viewport-theme combinations
    all_viewports = set()
    for flow_data in screenshots.values():
        all_viewports.update(flow_data.keys())

    sorted_viewports = sorted(all_viewports)

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Visual Documentation Dashboard - Family Assistant</title>
    <style>
        * {{ box-sizing: border-box; }}
        
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            margin: 0;
            padding: 20px;
            background-color: #f5f5f5;
            line-height: 1.6;
        }}
        
        .header {{
            background: white;
            padding: 30px;
            border-radius: 8px;
            margin-bottom: 30px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        
        .header h1 {{
            margin: 0 0 10px 0;
            color: #333;
        }}
        
        .header .meta {{
            color: #666;
            font-size: 14px;
        }}
        
        .navigation {{
            background: white;
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 20px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        
        .nav-links {{
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
        }}
        
        .nav-link {{
            background: #007acc;
            color: white;
            padding: 8px 16px;
            border-radius: 4px;
            text-decoration: none;
            font-size: 14px;
            transition: background-color 0.2s;
        }}
        
        .nav-link:hover {{
            background: #005a9e;
        }}
        
        .flow-section {{
            background: white;
            margin-bottom: 40px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            overflow: hidden;
        }}
        
        .flow-header {{
            background: #f8f9fa;
            padding: 20px;
            border-bottom: 1px solid #e9ecef;
        }}
        
        .flow-title {{
            margin: 0;
            color: #333;
            text-transform: capitalize;
        }}
        
        .viewport-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(400px, 1fr));
            gap: 20px;
            padding: 20px;
        }}
        
        .viewport-column {{
            border: 1px solid #e9ecef;
            border-radius: 6px;
            overflow: hidden;
        }}
        
        .viewport-header {{
            background: #007acc;
            color: white;
            padding: 15px;
            font-weight: 600;
            text-transform: capitalize;
        }}
        
        .screenshot-list {{
            padding: 15px;
        }}
        
        .screenshot-item {{
            margin-bottom: 20px;
            border: 1px solid #dee2e6;
            border-radius: 4px;
            overflow: hidden;
        }}
        
        .screenshot-item:last-child {{
            margin-bottom: 0;
        }}
        
        .screenshot-meta {{
            background: #f8f9fa;
            padding: 10px;
            font-size: 12px;
            color: #666;
            border-bottom: 1px solid #dee2e6;
        }}
        
        .screenshot-image {{
            width: 100%;
            height: auto;
            display: block;
            cursor: pointer;
        }}
        
        .screenshot-image:hover {{
            opacity: 0.9;
        }}
        
        .no-screenshots {{
            color: #999;
            font-style: italic;
            padding: 20px;
            text-align: center;
        }}
        
        .stats {{
            background: white;
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 20px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
        }}
        
        .stat-item {{
            text-align: center;
            padding: 15px;
            background: #f8f9fa;
            border-radius: 4px;
        }}
        
        .stat-number {{
            font-size: 24px;
            font-weight: bold;
            color: #007acc;
        }}
        
        .stat-label {{
            color: #666;
            font-size: 14px;
        }}
        
        .lightbox {{
            display: none;
            position: fixed;
            z-index: 1000;
            left: 0;
            top: 0;
            width: 100%;
            height: 100%;
            background-color: rgba(0,0,0,0.9);
            cursor: pointer;
        }}
        
        .lightbox img {{
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            max-width: 95%;
            max-height: 95%;
            border-radius: 4px;
        }}
        
        @media (max-width: 768px) {{
            .viewport-grid {{
                grid-template-columns: 1fr;
            }}
            
            .nav-links {{
                flex-direction: column;
            }}
            
            .stats-grid {{
                grid-template-columns: repeat(2, 1fr);
            }}
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>Visual Documentation Dashboard</h1>
        <div class="meta">
            Generated on {datetime.now().strftime("%Y-%m-%d %H:%M:%S")} • 
            Family Assistant UI Screenshots
        </div>
    </div>
    
    <div class="stats">
        <div class="stats-grid">
            <div class="stat-item">
                <div class="stat-number">{len(screenshots)}</div>
                <div class="stat-label">Flows Documented</div>
            </div>
            <div class="stat-item">
                <div class="stat-number">{len(sorted_viewports)}</div>
                <div class="stat-label">Viewport Combinations</div>
            </div>
            <div class="stat-item">
                <div class="stat-number">{sum(len(flow_data.get(vp, [])) for flow_data in screenshots.values() for vp in sorted_viewports)}</div>
                <div class="stat-label">Total Screenshots</div>
            </div>
        </div>
    </div>
    
    <div class="navigation">
        <div class="nav-links">
"""

    # Add navigation links for each flow
    for flow_name in sorted(screenshots.keys()):
        html_content += f'            <a href="#{flow_name}" class="nav-link">{flow_name.replace("-", " ").title()}</a>\\n'

    html_content += """        </div>
    </div>
"""

    # Generate sections for each flow
    for flow_name in sorted(screenshots.keys()):
        flow_data = screenshots[flow_name]

        html_content += f"""
    <div class="flow-section" id="{flow_name}">
        <div class="flow-header">
            <h2 class="flow-title">{flow_name.replace("-", " ").title()}</h2>
        </div>
        <div class="viewport-grid">
"""

        # Create columns for each viewport
        for viewport in sorted_viewports:
            screenshots_for_viewport = flow_data.get(viewport, [])

            html_content += f"""
            <div class="viewport-column">
                <div class="viewport-header">{viewport.replace("-", " ").title()}</div>
                <div class="screenshot-list">
"""

            if screenshots_for_viewport:
                for screenshot in screenshots_for_viewport:
                    html_content += f"""
                    <div class="screenshot-item">
                        <div class="screenshot-meta">
                            {screenshot["filename"]} • 
                            {screenshot["size"]:,} bytes • 
                            {screenshot["modified"].strftime("%H:%M:%S")}
                        </div>
                        <img src="{screenshot["path"]}" 
                             alt="{screenshot["filename"]}" 
                             class="screenshot-image"
                             onclick="showLightbox(this.src)">
                    </div>
"""
            else:
                html_content += '                    <div class="no-screenshots">No screenshots available</div>\\n'

            html_content += """                </div>
            </div>
"""

        html_content += """        </div>
    </div>
"""

    # Add lightbox and closing HTML
    html_content += """
    <div class="lightbox" id="lightbox" onclick="hideLightbox()">
        <img id="lightbox-image" src="" alt="">
    </div>
    
    <script>
        function showLightbox(src) {
            document.getElementById('lightbox-image').src = src;
            document.getElementById('lightbox').style.display = 'block';
        }
        
        function hideLightbox() {
            document.getElementById('lightbox').style.display = 'none';
        }
        
        // Close lightbox on escape key
        document.addEventListener('keydown', function(event) {
            if (event.key === 'Escape') {
                hideLightbox();
            }
        });
    </script>
</body>
</html>"""

    # Write the HTML file
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html_content, encoding="utf-8")
    print(f"✓ Generated dashboard: {output_path}")


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Generate visual documentation dashboard"
    )
    parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path("visual-docs"),
        help="Base directory containing screenshots (default: visual-docs)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("visual-docs/index.html"),
        help="Output HTML file path (default: visual-docs/index.html)",
    )

    args = parser.parse_args()

    print(f"Scanning screenshots in: {args.base_dir}")
    screenshots = scan_screenshots(args.base_dir)

    if not screenshots:
        print("No screenshots found. Run visual documentation tests first:")
        print(
            "GENERATE_VISUAL_DOCS=1 pytest -m visual_documentation tests/functional/web/test_visual_documentation.py"
        )
        return

    print(f"Found {len(screenshots)} flows with screenshots")

    generate_dashboard_html(screenshots, args.output)

    # Also save metadata as JSON for programmatic access
    metadata_path = args.output.parent / "metadata.json"
    metadata = {
        "generated_at": datetime.now().isoformat(),
        "flows": screenshots,
        "summary": {
            "total_flows": len(screenshots),
            "total_screenshots": sum(
                len(flow_data.get(vp, []))
                for flow_data in screenshots.values()
                for vp in set().union(*[
                    flow_data.keys() for flow_data in screenshots.values()
                ])
            ),
        },
    }

    metadata_path.write_text(
        json.dumps(metadata, indent=2, default=str), encoding="utf-8"
    )
    print(f"✓ Generated metadata: {metadata_path}")


if __name__ == "__main__":
    main()
