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
            from aggregator.country_config import get_config
            _cfg = get_config()
            dynamic_bias_sections = [f"How {_cfg['bias_labels'][i]} is covering it:" for i in range(1, 6)]
            
            next_sections = dynamic_bias_sections + [
                "How the left is covering it:", "Why it matters:",
                "What's the game or company:", "Key performances:",
                "Market impact:", "What experts are saying:",
                "Key details:", "Different perspectives:",
                "What the research shows:", "What the coverage is saying:",
                "The bigger picture:"
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

    @app.context_processor
    def inject_bias_helpers():
        from aggregator.country_config import get_config
        _cfg = get_config()
        
        def bias_label(score):
            if score is None: return ""
            bucket = int(round(score))
            if bucket < 1: bucket = 1
            if bucket > 5: bucket = 5
            return _cfg["bias_labels"][bucket]
            
        def bias_color_class(score):
            if score is None: return ""
            bucket = int(round(score))
            if bucket <= 2: return "bias-left"
            if bucket == 3: return "bias-center"
            return "bias-right"

        return dict(bias_label=bias_label, bias_color_class=bias_color_class)
