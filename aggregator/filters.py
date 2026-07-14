def register_filters(app):

    @app.template_filter("get_whats_happening")
    def get_whats_happening(summary):
        if not summary:
            return None
        try:
            start_marker = "What's happening:"
            end_marker = "What's next:"
            if start_marker in summary:
                parts = summary.split(start_marker)
                content = parts[1]
                if end_marker in content:
                    content = content.split(end_marker)[0]
                return content.strip()
        except Exception:
            pass
        return None

    @app.template_filter("get_the_story")
    def get_the_story(report):
        if not report:
            return None
        try:
            markers = [
                "The story:",
                "What happened:",
                "The discovery or development:",
                "The discovery:",
                "The development:"
            ]
            start_marker = None
            for m in markers:
                if m in report:
                    start_marker = m
                    break
            if not start_marker:
                return None

            next_sections = [
                "How the left is covering it:", "Why it matters:",
                "What's the game or company:", "Key performances:",
                "Market impact:", "What experts are saying:",
                "Key details:", "Different perspectives:",
                "What the research shows:", "What the coverage is saying:",
                "The bigger picture:", "What's missing:", "What's next:",
            ]
            content = report.split(start_marker)[1]
            end_pos = len(content)
            for next_m in next_sections:
                pos = content.find(next_m)
                if pos != -1 and pos < end_pos:
                    end_pos = pos
            return content[:end_pos].strip()
        except Exception:
            pass
        return None

    @app.template_filter("get_big_picture")
    def get_big_picture(summary):
        if not summary:
            return None
        try:
            start_marker = "The big picture:"
            end_marker = "Why it matters:"
            if start_marker in summary:
                parts = summary.split(start_marker)
                content = parts[1]
                if end_marker in content:
                    content = content.split(end_marker)[0]
                return content.strip()
        except Exception:
            pass
        return summary.strip()

    @app.template_filter("topic_color")
    def topic_color(name):
        """Pick a stable vivid color for a topic chip based on its name."""
        palette = [
            "#0891b2",  # cyan
            "#db2777",  # pink
            "#d97706",  # amber
            "#65a30d",  # lime
            "#7c3aed",  # violet
            "#dc2626",  # red
            "#059669",  # emerald
            "#2563eb",  # blue
        ]
        if not name:
            return palette[0]
        return palette[sum(ord(ch) for ch in name) % len(palette)]

    @app.template_global()
    def outlet_status(outlet):
        from aggregator.outlet_prefs import status_for_outlet
        return status_for_outlet(outlet)
