# Intent Classifier Documentation - Index

## 📚 Complete Documentation Package

This repository contains comprehensive documentation about the **Intent Classifier** in miraq-chat.

---

## 📄 Available Documents

### 1. 📊 **Presentation Slides** (Recommended Starting Point)
**File:** `INTENT_CLASSIFIER_SLIDES.md`

**What it is:** A complete 20-slide presentation covering the entire intent classification system

**Best for:**
- Presentations to stakeholders
- Technical overviews
- Training sessions
- Understanding the big picture

**Contents:**
- System overview and architecture
- Classification flow (step-by-step)
- All 40+ intent categories explained
- Entity extraction process
- LLM fallback mechanism
- Performance metrics
- Future roadmap
- Q&A section

**Time to read:** 30-45 minutes

---

### 2. 📖 **Quick Reference Guide**
**File:** `INTENT_CLASSIFIER_QUICK_REFERENCE.md`

**What it is:** A concise one-page overview of the key concepts

**Best for:**
- Quick lookup
- Developer onboarding
- Refreshing knowledge
- Getting started fast

**Contents:**
- High-level architecture diagram
- 3-step classification process
- Intent categories summary
- Example classifications
- Smart features list
- Performance highlights

**Time to read:** 5-10 minutes

---

### 3. 🔄 **Detailed Flow Diagram**
**File:** `INTENT_CLASSIFIER_FLOW_DIAGRAM.md`

**What it is:** A visual step-by-step breakdown of a complete classification request

**Best for:**
- Understanding implementation details
- Debugging issues
- Tracing execution flow
- Performance optimization

**Contents:**
- Complete 10-step flow visualization
- Real example: "show me 12x24 matte tiles in stock"
- Decision points explained
- API call generation details
- Performance breakdown with timing
- Bottleneck analysis

**Time to read:** 15-20 minutes

---

## 🎯 How to Use This Documentation

### For New Team Members
1. Start with **Quick Reference Guide** (5 min) - Get oriented
2. Read **Presentation Slides** (30 min) - Deep understanding
3. Refer to **Flow Diagram** (15 min) - See it in action

### For Stakeholders / Non-Technical
1. Read **Slides 1-5** from the presentation - System overview
2. Skim **Slides 13-16** - Key features and examples
3. Check **Quick Reference** - Summary

### For Developers
1. **Quick Reference** - Quick overview
2. **Flow Diagram** - Understand execution
3. **Slides 6-12** - Deep dive into components
4. Source code: `classifier.py`, `models.py`, `api_builder.py`

### For Debugging
1. **Flow Diagram** - Trace the request path
2. **Slides 7-8** - Understand priority logic
3. **Slides 10** - Check LLM fallback conditions
4. Logs in `/routes/chat.py`

### For Presentations
1. **Presentation Slides** - Complete deck ready to present
2. Can be converted to PowerPoint using tools like `pandoc`
3. Or present directly from Markdown viewers

---

## 🏗️ System Architecture Summary

```
┌────────────────────────────────────────────────────────┐
│                   MIRAQ-CHAT                            │
│              Intent Classification System               │
├────────────────────────────────────────────────────────┤
│                                                         │
│  ┌──────────────┐     ┌──────────────┐                │
│  │   Customer   │────▶│   Classifier │                │
│  │    Query     │     │   (Regex +   │                │
│  │              │     │      LLM)    │                │
│  └──────────────┘     └──────────────┘                │
│                              │                          │
│         ┌────────────────────┼────────────────────┐   │
│         │                    │                    │   │
│    ┌────▼─────┐      ┌──────▼──────┐     ┌──────▼────┐│
│    │ Entity   │      │   Intent    │     │    API    ││
│    │Extraction│      │Classification│    │  Builder  ││
│    └──────────┘      └─────────────┘     └───────────┘│
│         │                    │                    │    │
│         └────────────────────┴────────────────────┘    │
│                              │                          │
│                     ┌────────▼─────────┐               │
│                     │  WooCommerce API │               │
│                     └──────────────────┘               │
│                                                         │
└────────────────────────────────────────────────────────┘
```

**Key Components:**
1. **Entity Extraction** - Parse structured data from text
2. **Intent Classification** - Determine user's goal (40+ intents)
3. **API Builder** - Convert to WooCommerce API calls
4. **StoreLoader** - Dynamic catalog synchronization
5. **LLM Fallback** - AI-powered assistance when needed

---

## 📊 Quick Stats

- **Intents Supported:** 40+
- **Classification Speed:** 10-50ms
- **Accuracy:** 95%+ on common queries
- **LLM Fallback Rate:** 5-10% of queries
- **Test Coverage:** 160+ test cases
- **Lines of Code:** ~3,000 (classifier + related)

---

## 🔗 Related Files in Repository

### Core Implementation
- `classifier.py` - Main classification logic
- `models.py` - Data structures (Intent enum, entities)
- `api_builder.py` - API call generation
- `store_loader.py` - Dynamic catalog sync
- `llm_fallback.py` - AI fallback mechanism
- `response_generator.py` - Bot message generation

### Testing
- `test_sample_size_extraction.py` - Sample size tests
- `test_classifier_priority.py` - Priority logic tests
- `test_product_classification_bugs.py` - Bug fixes
- `test_greeting_intent.py` - Greeting tests
- `test_llm_fallback.py` - LLM fallback tests
- `test_conversation_flow.py` - Integration tests

### Documentation
- `README.md` - Project overview
- `IMPLEMENTATION_SUMMARY.md` - Recent changes
- `LLM_FALLBACK_GUIDE.md` - LLM integration guide
- **This package** - Intent classifier deep dive

---

## 🎓 Learning Path

### Beginner (Just getting started)
1. Read: **Quick Reference Guide**
2. Explore: `README.md` in repository
3. Try: Run `pytest` to see tests in action

### Intermediate (Want to contribute)
1. Read: **Presentation Slides** (full)
2. Study: **Flow Diagram**
3. Review: `classifier.py` source code
4. Run: `python -m training.evaluate` for accuracy

### Advanced (Deep dive)
1. All documentation above
2. Source code: All core files
3. Tests: Understand test coverage
4. Experiment: Modify patterns, add intents
5. Performance: Profile with logs

---

## 💡 Key Takeaways

### What Makes This Special?
✅ **Hybrid Approach** - Fast regex + smart LLM fallback
✅ **Domain-Optimized** - 40+ intents for e-commerce tile store
✅ **Dynamic** - No hardcoded data, auto-syncs with catalog
✅ **Privacy-Safe** - PII sanitization for LLM calls
✅ **Production-Ready** - High accuracy, fast, well-tested

### Common Use Cases
- Product search by name
- Category browsing with filters
- Attribute-based filtering (size, finish, color)
- Order tracking and reordering
- Sample requests
- Promotional queries

### When LLM Fallback Helps
- Complex natural language queries
- Ambiguous phrasing
- Typos or misspellings
- New product inquiries
- Edge cases not covered by regex

---

## 🤔 FAQ

**Q: Which document should I read first?**
A: Start with the **Quick Reference Guide** for a quick overview, then dive into **Presentation Slides** for details.

**Q: How do I present this to stakeholders?**
A: Use the **Presentation Slides** - they're designed for presentations and can be converted to PowerPoint.

**Q: I want to understand how it works internally.**
A: Read the **Flow Diagram** - it shows a complete example from start to finish with all decision points.

**Q: Can I modify these documents?**
A: Yes! They're in Markdown format. Edit freely and commit changes.

**Q: How do I convert Markdown to PowerPoint?**
A: Use tools like `pandoc`: `pandoc INTENT_CLASSIFIER_SLIDES.md -o slides.pptx`

---

## 📞 Need Help?

- **Questions?** Open an issue on GitHub
- **Bug reports?** Include query example and expected vs actual behavior
- **Feature requests?** Describe use case and propose solution
- **Contributing?** Read the code, add tests, submit PR

---

## 🚀 Next Steps

After reading these documents, you might want to:

1. **Run the system:** `python main.py`
2. **Run tests:** `pytest -v`
3. **Check accuracy:** `python -m training.evaluate`
4. **Explore code:** Start with `classifier.py`
5. **Try queries:** Use the `/chat` endpoint
6. **Add intents:** Follow the pattern in `models.py` + `classifier.py`

---

## 📝 Document Maintenance

**Last Updated:** 2026-02-19

**Version:** 1.0

**Authors:** Development Team

**Review Cycle:** Update when major changes to classifier

**Feedback:** Please report issues or suggestions via GitHub issues

---

## Summary

You now have a complete documentation package covering:
- ✅ High-level presentation (20 slides)
- ✅ Quick reference guide (1 page)
- ✅ Detailed flow diagram (complete example)

**Total reading time:** 50-75 minutes for everything

**Use cases:** Presentations, onboarding, debugging, planning

**Format:** Markdown (easily converted to PDF, PowerPoint, HTML)

Happy learning! 🎓
