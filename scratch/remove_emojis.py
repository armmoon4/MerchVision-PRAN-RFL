import re
import sys

sys.stdout.reconfigure(encoding='utf-8')

def main():
    with open('testui.html', 'r', encoding='utf-8') as f:
        content = f.read()

    # Exact string replacements for UI labels and text
    exact_replacements = [
        ('📸 Shelf AI Audit (Image → Insights)', 'Shelf AI Audit (Image to Insights)'),
        ('📊 Management Dashboard', 'Management Dashboard'),
        ('🔬 AI Vision Lab &amp; SKUs', 'AI Vision Lab &amp; SKUs'),
        ('📦 ItemsDB Catalogue (1,954 SKUs)', 'ItemsDB Catalogue (1,954 SKUs)'),
        ('⚡ API Telemetry &amp; Costs', 'API Telemetry &amp; Costs'),
        ('<span>🧠 Vision AI Perception Findings</span>', '<span>Vision AI Perception Findings</span>'),
        ('<span>🏷️ Shelf Pricing &amp; POSM Wobblers</span>', '<span>Shelf Pricing &amp; POSM Wobblers</span>'),
        ('<span>☑ Recommended actions</span>', '<span>Recommended Actions</span>'),
        ('📷 Shelf Photo Input &amp; Studio', 'Shelf Photo Input &amp; Studio'),
        ('📷 Shelf Photo Input', 'Shelf Photo Input'),
        ('⚡ Analyze Shelf Photo', 'Analyze Shelf Photo'),
        ('📸 Sample Image', 'Sample Image'),
        ('📋 Wafer Audit Demo', 'Wafer Audit Demo'),
        ('📈 Executive Directives &amp; Shelf Observations', 'Executive Directives &amp; Shelf Observations'),
        ('🔍 Detected Shelf Products Explorer', 'Detected Shelf Products Explorer'),
        ('🔍 Detected Shelf Products', 'Detected Shelf Products'),
        ('<span>📋 Full AI Response Data (JSON)</span>', '<span>Full AI Response Data (JSON)</span>'),
        ('📋 AI Response Data (JSON)', 'AI Response Data (JSON)'),
        ('🚨 Exception-First Outlets (High Urgency Ranking)', 'Exception-First Outlets (High Urgency Ranking)'),
        ('🔬 Direct Vision &amp; Catalogue Matcher Lab', 'Direct Vision &amp; Catalogue Matcher Lab'),
        ('📦 PRAN ItemsDB Catalogue Explorer', 'PRAN ItemsDB Catalogue Explorer'),
        ('⚡ API Telemetry &amp; Token Economics', 'API Telemetry &amp; Token Economics'),
        ('↻ Refresh Dashboard', 'Refresh Dashboard'),
        ('⏳ Analyzing Shelf with AI...', 'Analyzing Shelf with AI...'),
        ('✓ Copied!', 'Copied!'),
        ('<span>⚠️</span> <span>${escapeHtml(d)}</span>', '<span style="font-weight:700; color:#f87171;">[Deduction]</span> <span>${escapeHtml(d)}</span>'),
        ('🏷️ Promo / Wobbler Active', 'Promo / Wobbler Active'),
        ('🏷️', ''),
        ('✓ No Critical Issues Flagged', 'No Critical Issues Flagged'),
        ('<span>📍 Level:', '<span>Level:'),
        ('<span style="color:#38bdf8; font-size:14px;">💡</span>', '<span class="pill pill-blue" style="font-size:10px; padding:2px 6px;">NOTE</span>'),
        ('<span style="color:#fbbf24; font-size:14px;">📐</span>', '<span class="pill pill-amber" style="font-size:10px; padding:2px 6px;">BOUND</span>'),
        ('<span>⚡ <strong>Gemini 3.7 Vision Telemetry:</strong>', '<span><strong>Gemini 3.7 Vision Telemetry:</strong>'),
    ]

    for old, new in exact_replacements:
        if old in content:
            content = content.replace(old, new)

    # Replace key findings JS logic to use clean pill badges instead of emojis
    old_findings_js = """          findingsList.innerHTML = keyFindings.map(f => {
            let icon = '🔹';
            const fLower = f.toLowerCase();
            if (fLower.includes('promot') || fLower.includes('wobbler') || fLower.includes('beli & menang')) icon = '🏷️';
            else if (fLower.includes('competitor') || fLower.includes('occupy')) icon = '⚔️';
            else if (fLower.includes('short') || fLower.includes('deficit') || fLower.includes('target')) icon = '📉';
            else if (fLower.includes('pran') || fLower.includes('detected')) icon = '📦';
            return `
              <div class="finding-item">
                <span class="finding-icon">${icon}</span>
                <span class="finding-text">${escapeHtml(f)}</span>
              </div>
            `;
          }).join('');"""

    new_findings_js = """          findingsList.innerHTML = keyFindings.map(f => {
            let tag = '<span class="pill pill-blue" style="font-size:10px; padding:2px 6px;">INFO</span>';
            const fLower = f.toLowerCase();
            if (fLower.includes('promot') || fLower.includes('wobbler') || fLower.includes('beli & menang')) {
              tag = '<span class="pill pill-green" style="font-size:10px; padding:2px 6px;">PROMO</span>';
            } else if (fLower.includes('competitor') || fLower.includes('occupy')) {
              tag = '<span class="pill pill-amber" style="font-size:10px; padding:2px 6px;">COMP</span>';
            } else if (fLower.includes('short') || fLower.includes('deficit') || fLower.includes('target')) {
              tag = '<span class="pill pill-red" style="font-size:10px; padding:2px 6px;">SOS</span>';
            } else if (fLower.includes('pran') || fLower.includes('detected')) {
              tag = '<span class="pill pill-blue" style="font-size:10px; padding:2px 6px;">FACING</span>';
            }
            return `
              <div class="finding-item">
                <span class="finding-icon">${tag}</span>
                <span class="finding-text">${escapeHtml(f)}</span>
              </div>
            `;
          }).join('');"""

    if old_findings_js in content:
        content = content.replace(old_findings_js, new_findings_js)
        print("Replaced findings JS with pill badges.")

    # Any remaining emojis regex
    emoji_pattern = re.compile(
        r'[\U00010000-\U0010ffff]|[\u2600-\u27bf]|[\u2300-\u23ff]|[\u2b50-\u2b55]|[\ufe00-\ufe0f]'
    )

    remaining = emoji_pattern.findall(content)
    if remaining:
        print(f"Remaining emojis found: {len(remaining)}. Removing them...")
        content = emoji_pattern.sub('', content)

    with open('testui.html', 'w', encoding='utf-8') as f:
        f.write(content)

    print("Emoji removal complete. File testui.html updated.")

if __name__ == '__main__':
    main()
