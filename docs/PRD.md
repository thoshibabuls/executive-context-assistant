
# Product Requirements Document
## Executive Context Assistant

**Working title:** Executive Context Assistant  
**Document type:** Product Requirements Document  
**Status:** MVP Definition  
**Version:** 1.0  
**Primary user:** CEO / CTO / Director / senior knowledge worker  
**Initial ecosystem:** Google Workspace  
**Future ecosystems:** Microsoft 365, Slack, Teams, other enterprise productivity platforms

---

# 1. Executive Summary

The Executive Context Assistant is an AI-powered personal work assistant designed to work alongside a senior professional throughout their working day.

Its purpose is not simply to summarize emails or answer questions.

Its purpose is to continuously maintain the user's **work context** across communication, meetings, tasks, commitments, deadlines, people, projects, and decisions.

A senior executive may communicate with dozens of people across email, chat, meetings, and documents. Important commitments and decisions become distributed across these systems. The user is therefore forced to repeatedly reconstruct context:

- Who did I speak to?
- What did we agree on?
- What did I promise?
- What are they supposed to send me?
- What am I waiting for?
- What did we discuss yesterday?
- Which messages require my attention?
- Which people should I respond to first?
- What meetings are coming up?
- What happened in previous meetings?
- What deadlines are approaching?
- What tasks are still open?

The product acts as a continuously maintained **personal context layer** across these workflows.

The central product promise is:

> **Your work context, continuously remembered.**

The assistant should help the user move from:

**"I have too much information."**

to:

**"I know what matters, what happened, what I owe, what others owe me, and what I should do next."**

---

# 2. Product Vision

Professionals should not need to rely on their memory to manage complex communication and relationships.

The Executive Context Assistant creates a living representation of the user's work context and makes the relevant information available at the moment it matters.

The long-term vision is to create a **personal operating layer for knowledge workers** that sits above existing productivity applications rather than replacing them.

The assistant should eventually work across:

- Email
- Chat
- Meetings
- Calendar
- Documents
- Tasks
- Contacts
- Projects
- External relationships

The underlying product should therefore remain **platform-independent**.

Google Workspace is the initial implementation environment, not the definition of the product.

---

# 3. Problem Statement

Senior professionals operate across fragmented information systems.

A single business relationship may span:

- several emails
- chat messages
- meetings
- documents
- calendar events
- tasks
- commitments
- follow-ups

The information exists, but the relationships between the information are difficult to maintain.

Existing productivity tools generally organize information by application:

**Gmail → email**

**Calendar → meetings**

**Tasks → tasks**

**Drive → documents**

**Chat → conversations**

The user's actual work, however, does not exist in these separate categories.

A real work situation looks more like:

> Person → conversation → project → decision → commitment → deadline → follow-up → meeting → new conversation.

The product therefore needs to organize context around the **user's work**, not around the underlying applications.

---

# 4. Target User

## Primary persona

### CEO / CTO / Director / Senior Executive

Characteristics:

- high volume of communication
- multiple simultaneous projects
- many internal and external stakeholders
- frequent meetings
- frequent commitments
- limited time
- high cost of missing important information
- needs rapid context switching
- communicates across multiple channels
- frequently delegates tasks
- frequently receives requests from people with different levels of importance

---

# 5. What a High-Quality Executive Assistant Actually Does

The product should model the useful responsibilities of a strong executive assistant rather than simply becoming an AI chatbot.

Research into executive-assistant responsibilities consistently emphasizes:

- calendar and schedule management
- meeting preparation
- follow-ups
- tracking commitments
- tracking owners and deadlines
- prioritizing requests
- identifying delays
- maintaining action trackers
- preparing daily/weekly summaries
- information gathering
- protecting executive attention
- ensuring important actions are not forgotten

One current executive-assistant role description explicitly frames the central question as:

> "What needs to happen next, who is responsible, and by when will it be completed?"

The product should therefore organize its capabilities around five executive-assistant responsibilities:

### 5.1 Understand

Understand what is happening.

### 5.2 Remember

Maintain relevant historical context.

### 5.3 Prioritize

Determine what deserves attention.

### 5.4 Remind

Prevent important commitments from being forgotten.

### 5.5 Recommend

Help the user decide what to do next.

---

# 6. Product Principles

## Principle 1 — Context before generation

The assistant must retrieve relevant context before generating important responses.

It should not behave like a generic chatbot.

---

## Principle 2 — The user works normally

The user should not have to manually maintain the assistant's memory.

The system should derive context from existing workflows whenever possible.

---

## Principle 3 — No invented commitments

The system must distinguish between:

- explicit commitment
- probable commitment
- suggestion
- inferred task
- confirmed task

It must never present an inference as a fact.

---

## Principle 4 — Every important answer should be traceable

When possible, the assistant should be able to show where information came from:

- email
- meeting
- chat
- document
- calendar event

The user should be able to inspect the source.

---

## Principle 5 — Attention is expensive

The assistant should minimize unnecessary notifications.

A system that flags everything is not useful.

---

## Principle 6 — Cost is a product requirement

AI inference should not be used unnecessarily.

Simple operations should use inexpensive processing.

Complex reasoning should only occur when necessary.

---

## Principle 7 — Platform independence

The product must separate:

**Product logic**

from

**Data-source connectors.**

The initial Google implementation must not make the core product dependent on Google-specific concepts.

---

# 7. Core Product Model

The assistant should conceptually maintain the following objects.

### Person

Who is involved?

### Organization

Where do they belong?

### Conversation

What was discussed?

### Meeting

What happened?

### Project

What larger workstream does this belong to?

### Task

What needs to be done?

### Commitment

Who promised what?

### Deadline

When does something need to happen?

### Decision

What was decided?

### Relationship

How are people, organizations, projects and conversations connected?

### Priority

How important is this to the user?

These objects form the user's **work context**.

---

# 8. Core User Experience

The product should have five primary surfaces:

1. **Today**
2. **Chat**
3. **Tasks**
4. **People**
5. **Meetings**

Email and communication integrations should feed information into these surfaces.

---

# 9. Feature 1 — Today Dashboard

The dashboard is the user's executive briefing.

The user should be able to open the product and immediately understand:

> **What matters today?**

The dashboard should contain:

### Today

- important meetings
- urgent tasks
- overdue tasks
- approaching deadlines
- high-priority people requiring responses
- pending commitments
- important unanswered communication
- newly detected action items

### Attention Required

Example:

> **3 things need your attention**

1. Microsoft security documentation — due tomorrow
2. Reply to CFO regarding project budget
3. Review proposal before 4 PM meeting

### Waiting For

Example:

> **Waiting on 4 people**

- John — security documentation
- Sarah — contract revision
- Vendor X — proposal
- Engineering — production estimate

### Your Commitments

Example:

> **You committed to 3 things**

- Send architecture document — today
- Review proposal — tomorrow
- Schedule workshop — Friday

### Upcoming

- meetings
- deadlines
- important follow-ups

---

# 10. Feature 2 — AI Assistant Chat

Chat is the primary natural-language interface to the user's context.

The assistant should answer questions about:

### Current context

> What do I need to focus on today?

### Historical context

> What did John and I discuss last week?

### Commitments

> What have I promised Microsoft?

### Open tasks

> What am I waiting for?

### Deadlines

> What deadlines are coming up this week?

### People

> Who are the most important people I haven't replied to?

### Meetings

> What happened in yesterday's meeting?

### Cross-context questions

> What did we decide yesterday that relates to today's meeting?

### Action-oriented questions

> What should I do next?

### Communication questions

> What should I reply to John?

The assistant should prefer concise answers with supporting context and source references.

---

# 11. Cross-Context Reasoning

This is one of the most important differentiators.

The assistant must not treat each conversation independently.

Example:

### Monday

Meeting:

> Microsoft will provide security documentation.

### Tuesday

Email:

> "Following up on the documentation..."

### Wednesday

User asks:

> "What is the status of the security review?"

The assistant should connect the three events.

Expected response:

> The security review is waiting on Microsoft.
>
> In Monday's meeting, John committed to providing the security documentation.
>
> Their latest email indicates the documentation is still pending.
>
> Expected deadline: October 8.
>
> No completion evidence has been detected yet.

This is **context continuity**.

---

# 12. Feature 3 — Email Intelligence

Email processing is a major MVP capability.

The system should periodically analyze relevant emails and produce:

### Daily Email Summary

Example:

> **Today's communication**
>
> 38 emails received  
> 7 require attention  
> 4 contain potential action items  
> 2 contain deadlines  
> 1 requires a response today

The system should identify:

- important messages
- action items
- requests
- commitments
- deadlines
- unanswered messages
- follow-ups
- important people
- changes in existing tasks

---

# 13. Email Action Extraction

Example email:

> "Can you review the proposal and send your feedback before Friday?"

The system should extract:

**Task:** Review proposal

**Owner:** User

**Deadline:** Friday

**Source:** Email

**Status:** Open

**Confidence:** High

The dashboard should then surface it.

---

# 14. Email Priority

Not all emails should receive equal attention.

Priority should consider multiple signals:

### Sender importance

- executive
- client
- investor
- manager
- partner
- key stakeholder
- colleague
- low-priority contact

### Relationship importance

How important is this person/project to the user?

### Urgency

Does the message contain:

- deadline
- escalation
- explicit urgency
- time-sensitive request?

### Business impact

Could ignoring the message materially affect:

- project delivery
- revenue
- customer
- relationship
- decision
- risk?

### Required action

Does the user need to:

- reply
- review
- approve
- decide
- provide information?

### Historical context

Is this part of an existing active conversation?

The system should use these signals to produce a priority score.

---

# 15. Priority Should Be Explainable

The system should never simply say:

> "High priority."

It should be able to explain:

> **High priority because:**
>
> - Sender is a key external partner
> - Message contains a deadline tomorrow
> - You previously committed to respond
> - This relates to an active project

This makes the prioritization trustworthy.

---

# 16. Feature 4 — Task & Commitment Tracker

Tasks should be automatically extracted from:

- emails
- meetings
- uploaded recordings
- chat
- user-created tasks
- assistant conversations

The system should distinguish:

### My tasks

Things the user needs to do.

### Delegated tasks

Things the user asked another person to do.

### Waiting-for items

Things another person promised to do.

### Shared tasks

Tasks involving multiple people.

---

# 17. Task Lifecycle

Every task should have:

```text
Detected
   ↓
Suggested
   ↓
Confirmed
   ↓
Open
   ↓
In Progress
   ↓
Completed
```

The user should be able to manually correct the state.

The system must not automatically mark important work complete simply because it sees a related message.

---

# 18. Commitment Tracking

Commitments are distinct from generic tasks.

Example:

> "I'll send you the report by Friday."

The system records:

**Commitment**

User → send report → Friday

Another example:

> "We'll send the contract tomorrow."

The system records:

**External commitment**

Partner → send contract → tomorrow

This allows the assistant to answer:

> "What am I waiting for?"

and:

> "What am I supposed to deliver?"

---

# 19. Feature 5 — Reminders

The reminder system should focus on meaningful reminders rather than generating notification spam.

Reminder types:

### Deadline reminder

> Security documentation due tomorrow.

### Commitment reminder

> You promised to send the architecture document today.

### Waiting-for reminder

> Microsoft has not yet provided the documentation they committed to.

### Follow-up reminder

> You haven't received a response from John after your last message.

### Meeting preparation reminder

> Your meeting with Microsoft starts in 30 minutes. There are 3 unresolved items from your previous discussion.

---

# 20. Reminder Intelligence

The system should consider:

- deadline proximity
- importance
- user priority
- project importance
- relationship importance
- task age
- previous reminders
- whether the task has already been acted upon

The system should suppress repeated reminders when nothing has materially changed.

---

# 21. Feature 6 — Meetings

Meetings should be represented as first-class objects.

For the MVP, the user should be able to upload:

- meeting audio
- meeting video
- meeting transcript

The system processes the meeting and extracts:

- summary
- participants
- topics
- decisions
- tasks
- commitments
- deadlines
- unresolved questions
- follow-ups

---

# 22. Meeting Summary

Example:

### Microsoft Architecture Meeting

**Summary**

The teams discussed migration architecture and security requirements.

**Decisions**

- Use architecture approach A.
- Security review must happen before production migration.

**Your tasks**

- Send architecture diagram by Friday.

**Their tasks**

- Provide security documentation by October 8.

**Open questions**

- Authentication strategy remains unresolved.

---

# 23. Meeting Chat

The user should be able to ask questions specifically about a meeting.

Examples:

> What did John say about security?

> What did I agree to?

> What did we decide?

> Who owns the architecture document?

> What are the unresolved issues?

> What deadlines were mentioned?

---

# 24. Meeting-to-Meeting Context

The meeting assistant must not isolate meetings.

Example:

### Yesterday

Architecture meeting:

> Authentication strategy unresolved.

### Today

User asks:

> What should I ask about authentication in today's meeting?

The assistant should retrieve yesterday's meeting context and answer accordingly.

This is a critical product requirement.

---

# 25. Uploaded Missed Meeting

If the user missed a meeting, they should be able to upload the recording.

The system should generate:

### What happened?

### What was decided?

### What concerns were raised?

### What do I need to know?

### What do I owe?

### What does everyone else owe?

### What changed since the previous meeting?

This allows the user to recover context without manually watching the entire recording.

---

# 26. Feature 7 — People Intelligence

The assistant should maintain lightweight relationship context.

For each important person:

### Person profile

- name
- organization
- role
- relationship type
- recent interactions
- active projects
- open commitments
- outstanding requests
- last communication
- next expected action

Example:

### John Smith

Microsoft

**Current relationship topics**

- Cloud migration
- Security
- Contract

**Open items**

- Microsoft security documentation — Oct 8
- Architecture review — pending

**Your open items**

- Send architecture document — Oct 5

---

# 27. People Priority

The assistant should help the user determine who requires attention.

Priority should consider:

- relationship importance
- current project importance
- message urgency
- deadline
- explicit request
- business impact
- unresolved commitments
- role/seniority
- historical importance
- user's manually configured priority

The system should allow the user to override AI-generated priority.

---

# 28. Communication Priority Assistant

This is a major differentiating feature.

Suppose the user has 40 Teams/chat messages.

Instead of requiring them to read everything, the assistant should produce:

### Reply priority

**1. John — Microsoft**

Reason:
- deadline tomorrow
- active project
- waiting on your response

**2. CFO**

Reason:
- budget approval requested
- meeting at 4 PM

**3. Engineering Lead**

Reason:
- production blocker

Then the user can ask:

> What should I reply to John?

The assistant retrieves the relevant conversation history and generates guidance.

---

# 29. Reply Guidance

The assistant should not automatically send messages in the MVP.

Instead:

### Context

> John is following up on the security documentation.

### Previous agreement

> You agreed to send the architecture document.

### Current status

> You have not yet sent it.

### Suggested response

The assistant can generate a draft.

The user must approve before sending.

---

# 30. Feature 8 — Executive Briefing

The system should eventually provide a daily briefing.

Example:

# Good morning

### Today's priorities

1. Complete architecture document
2. Review budget proposal
3. Follow up with Microsoft

### Important meetings

10:00 — Engineering

2:00 — Microsoft

4:00 — CFO

### Waiting on

- Microsoft — security documentation
- Vendor X — pricing

### Deadlines

- Architecture document — today
- Microsoft documentation — Oct 8

### People requiring attention

- John — Microsoft
- CFO
- Engineering Lead

---

# 31. Dashboard Information Hierarchy

The dashboard should prioritize:

1. **Urgent action**
2. **Today's commitments**
3. **Important unanswered communication**
4. **Upcoming meetings**
5. **Waiting-for items**
6. **Upcoming deadlines**
7. **General context**

The dashboard should not become a generic information wall.

---

# 32. Flagging System

The system needs a consistent internal priority model.

Every detected item should receive a:

### Priority score

based on:

- urgency
- importance
- deadline
- relationship importance
- business impact
- commitment status
- user preferences
- confidence

Conceptually:

```text
Priority =
Urgency
+ Importance
+ Relationship Weight
+ Deadline Weight
+ Business Impact
+ Commitment Weight
```

The exact implementation should be determined during technical design.

The product requirement is:

> **Prioritization must be explainable, configurable, and measurable.**

---

# 33. Confidence vs Priority

These must be separate.

### Confidence

How certain is the system that it correctly extracted the information?

### Priority

How important is the information to the user?

Example:

> "Maybe the user promised to send the document."

Confidence: Low

Priority: Potentially High

The system should therefore avoid silently converting uncertain information into a confirmed task.

---

# 34. User Controls

The user should be able to:

- confirm task
- reject task
- edit deadline
- change priority
- assign person
- mark completed
- snooze reminder
- dismiss reminder
- mark person important
- mark project important
- correct AI interpretation

These corrections should improve future prioritization and extraction.

---

# 35. Context Management

The product must treat context as a first-class system.

The assistant should maintain different levels of context:

### Immediate context

Current conversation/question.

### Session context

Current user interaction.

### Recent context

Recent emails, meetings, chats and tasks.

### Persistent context

Important people, projects, commitments, decisions and preferences.

### Historical context

Older information retrieved when relevant.

The system should not send the entire user's history to the model on every request.

It should retrieve the smallest useful context for the current task.

This follows current context-engineering principles and Google guidance around separating retrieval from generation and controlling context size.

---

# 36. Source Grounding

Important answers should provide source references.

Example:

> You promised John the architecture document by Friday.

**Source:**  
Microsoft meeting — Sept 29  
Email — Oct 1

The assistant should distinguish:

**Known from source**

from

**AI inference**

from

**user preference**

---

# 37. Cost Optimization Requirement

Cost optimization is a first-class requirement.

The application should avoid using the most capable model for every operation.

Current Google documentation explicitly recommends choosing models according to the workload and starting with cost-efficient models before escalating to more powerful models.

The current Gemini API also provides lower-cost Flash-Lite models specifically for high-volume/simple processing, while more capable Flash models are available for complex reasoning.

Therefore the product should conceptually use:

### Cheap processing

For:

- classification
- simple extraction
- email categorization
- duplicate detection
- basic task extraction
- metadata extraction

### More capable processing

For:

- complex cross-meeting reasoning
- ambiguous commitments
- multi-source questions
- reply guidance
- difficult prioritization
- executive briefing synthesis

### Retrieval before generation

Do not send large amounts of irrelevant context to the model.

### Batch processing

Where latency is not important, batch processing should be considered.

### Caching

Repeated stable context should be cached where economically appropriate.

### Incremental processing

Do not repeatedly process unchanged emails, meetings or documents.

Google's current Gemini pricing documentation also exposes batch processing and context caching as cost-management mechanisms.

---

# 38. MVP Scope

The first MVP should optimize for:

**fast to build + cheap to operate + demonstrates the core product hypothesis.**

## MVP includes

### Authentication

Google account authentication.

### Gmail

- ingest emails
- summarize
- extract tasks
- extract commitments
- detect deadlines
- detect priority
- generate daily digest

### Calendar

- upcoming meetings
- meeting context
- meeting preparation

### Uploaded meetings

- upload audio/video
- transcribe/process
- summarize
- extract tasks
- extract commitments
- extract deadlines
- meeting chat

### Context assistant

- cross-email questions
- cross-meeting questions
- task questions
- people questions
- deadline questions
- cross-day context

### Task tracker

- open tasks
- delegated tasks
- waiting-for tasks
- commitments
- deadlines
- status

### Reminder engine

- approaching deadlines
- overdue tasks
- waiting-for items
- important follow-ups

### Dashboard

- today's priorities
- tasks
- reminders
- meetings
- people
- waiting-for
- commitments

### People intelligence

- important contacts
- recent interaction
- open items
- relationship context

---

# 39. MVP Explicitly Does NOT Include

Do not initially build:

- automatic email sending
- autonomous external communication
- fully autonomous task execution
- complex multi-agent architecture
- enterprise-wide organizational graph
- Microsoft Teams integration
- Microsoft Graph integration
- Slack integration
- CRM integration
- advanced predictive analytics
- autonomous calendar modification
- automatic deletion of user data
- complex multi-user collaboration

These can come later.

---

# 40. MVP Success Criteria

The MVP succeeds if a user can connect their Google account and ask:

> "What do I need to do today?"

and receive a useful answer.

It should also answer:

> "What am I waiting for?"

> "What did I promise?"

> "What happened in yesterday's meeting?"

> "What should I know before today's meeting?"

> "Who should I reply to first?"

> "What deadlines are coming up?"

> "What did we decide last week about X?"

---

# 41. Evaluation Framework

The product must not be judged only by whether the UI works.

AI quality must be measured.

Google recommends using golden datasets, LLM-as-judge where appropriate, user feedback, and standardized evaluation pipelines for generative-AI applications.

The MVP should create a small curated evaluation dataset.

---

# 42. Core AI Metrics

## Retrieval relevance

Does the system retrieve the information needed to answer the question?

Measure:

- Recall@K
- Precision@K
- NDCG where appropriate

---

## Groundedness

Does the answer actually follow from retrieved evidence?

---

## Factual accuracy

Did the system correctly represent:

- person
- task
- deadline
- commitment
- decision
- meeting

---

## Task extraction accuracy

Measure:

- precision
- recall
- false-positive rate

False positives are particularly important.

---

## Deadline extraction accuracy

Measure:

- correct date
- correct owner
- correct source

---

## Priority accuracy

Compare AI priority against human-labelled priority.

---

## Reminder quality

Measure:

### Precision

Percentage of reminders that users consider useful.

### False-positive rate

Percentage of reminders users dismiss as unnecessary.

### Miss rate

Important events that should have been surfaced but weren't.

---

## Chat quality

Measure:

- factual correctness
- relevance
- source grounding
- completeness
- latency
- cost

---

# 43. Context Continuity Metric

Create a specific evaluation category for cross-context questions.

Example test:

### Source 1

Meeting on Monday:

> John will send documentation by Friday.

### Source 2

Email on Wednesday:

> Still working on the document.

### Query

> What is the current status of John's commitment?

The system should connect the two.

This should become one of the core benchmark categories.

---

# 44. Executive Usefulness Metric

The most important product-level metric should eventually be:

> **Does the assistant reduce the user's effort required to reconstruct work context?**

Possible measurement:

Before assistant:

> Time required to determine what needs attention today.

After assistant:

> Time required to determine what needs attention today.

Additional user metric:

> Percentage of surfaced items that the user considers actionable.

---

# 45. Cost Metrics

Every AI operation should be observable.

Track:

- model
- input tokens
- output tokens
- latency
- operation type
- retrieval size
- estimated cost
- cache hit/miss
- retry count

Measure:

### Cost per active user/day

### Cost per email processed

### Cost per meeting processed

### Cost per chat query

### Cost per useful answer

The product should optimize for **cost per useful outcome**, not simply lowest model price.

---

# 46. Non-Functional Requirements

## Performance

Interactive chat should feel responsive.

Background processing may be asynchronous.

---

## Reliability

A temporary failure in one integration should not destroy the user's existing context.

---

## Security

The system must enforce source-level authorization.

A user should never receive information they did not authorize the system to access.

---

## Privacy

The system should clearly communicate:

- what data is connected
- what data is stored
- what data is processed
- what data is retained
- how users can remove data

---

## Auditability

Important AI-generated tasks, commitments and decisions should retain their source references.

---

# 47. Integration Architecture Requirement

The product must use an integration abstraction.

Conceptually:

```text
                    Context Assistant
                           |
                    Integration Layer
                           |
          +----------------+----------------+
          |                |                |
       Gmail            Outlook          Slack
          |                |                |
       Google           Microsoft         Slack
       Calendar         Teams
       Drive            SharePoint
```

The product core should not depend directly on Gmail-specific data structures.

Instead, connectors should normalize external data into product concepts such as:

```text
Message
Person
Meeting
Document
Task
Commitment
Deadline
Organization
```

---

# 48. Initial Google Implementation

For the first implementation:

### Data sources

- Gmail
- Google Calendar
- Google Drive where required
- uploaded meeting files

### AI

Use Google Gemini models.

The current Google model catalog includes low-cost Flash-Lite models aimed at high-volume/simple processing and more capable Flash models for complex workloads.

The exact model-routing strategy should be determined during architecture design and validated against the evaluation set.

---

# 49. Future Microsoft Implementation

The product should eventually support:

- Microsoft Graph
- Outlook
- Teams
- Microsoft Calendar
- SharePoint
- OneDrive

The Microsoft implementation should reuse the same normalized context model.

The product should not need to be redesigned merely because the connector changes.

---

# 50. Product Workflow

The intended lifecycle is:

```text
USER WORKS
     ↓
DATA SOURCES
     ↓
INGEST
     ↓
NORMALIZE
     ↓
EXTRACT
     ↓
UPDATE CONTEXT
     ↓
RETRIEVE RELEVANT CONTEXT
     ↓
PRIORITIZE
     ↓
ASSIST USER
     ↓
USER CORRECTS / CONFIRMS
     ↓
CONTEXT IMPROVES
```

---

# 51. Important Design Decision

The system should not attempt to make every decision autonomously.

There should be three levels:

### Level 1 — Inform

> "This email appears important."

### Level 2 — Recommend

> "You may want to reply to John before the CFO."

### Level 3 — Act

> "Send the email."

MVP should primarily support Levels 1 and 2.

Level 3 should require explicit user confirmation.

---

# 52. User Feedback Loop

User actions provide signals.

Examples:

User:

> Rejects task

System learns that extraction was incorrect.

User:

> Marks person important

System learns relationship priority.

User:

> Snoozes reminder

System learns timing preference.

User:

> Marks message low priority

System learns prioritization preference.

This should improve personalization without allowing uncontrolled behavior.

---

# 53. MVP Delivery Phases

## Phase 1 — Context Foundation

Build:

- authentication
- Gmail ingestion
- normalized data model
- people
- messages
- basic tasks
- deadlines
- basic dashboard

Goal:

> Establish the user's basic work context.

---

## Phase 2 — Assistant

Build:

- chat
- contextual retrieval
- cross-email questions
- task questions
- people questions
- commitment questions
- source references

Goal:

> Prove that the assistant can recover context.

---

## Phase 3 — Executive Intelligence

Build:

- priority
- email digest
- daily briefing
- reminders
- waiting-for
- communication prioritization

Goal:

> Move from "remembering" to "helping."

---

## Phase 4 — Meeting Intelligence

Build:

- video/audio upload
- meeting processing
- summaries
- tasks
- commitments
- deadlines
- meeting chat
- cross-meeting reasoning

Goal:

> Connect meeting context with the rest of the user's work.

---

## Phase 5 — Integration Expansion

Add:

- Microsoft 365
- Teams
- Slack
- other productivity platforms

Goal:

> Make the context layer platform-independent.

---

# 54. Future Product Expansion

Potential future capabilities:

### Executive briefing

Automatic morning briefing.

### Meeting preparation

Generate context before meetings.

### Relationship intelligence

Identify neglected relationships.

### Delegation tracking

Track whether delegated tasks are actually completed.

### Decision memory

Remember why decisions were made.

### Project intelligence

Automatically construct project context.

### Proactive assistant

Surface important context without requiring a question.

### Communication copilot

Help draft responses using full relationship history.

### Autonomous workflows

Eventually perform approved actions.

---

# 55. Major Product Risks

## Risk 1 — Too many false positives

If everything is flagged as important, nothing is important.

Mitigation:

- confidence thresholds
- priority thresholds
- user feedback
- reminder suppression
- evaluation dataset

---

## Risk 2 — Hallucinated commitments

A model may infer that someone promised something when they did not.

Mitigation:

- explicit source grounding
- confidence labels
- confirmation workflow
- commitment extraction evaluation

---

## Risk 3 — Context overload

Retrieving too much information can make answers worse and more expensive.

Mitigation:

- context budgets
- relevance filtering
- hierarchical retrieval
- summarization
- caching

---

## Risk 4 — High inference cost

Processing every email and meeting with an expensive model is economically inefficient.

Mitigation:

- model routing
- batch processing
- incremental processing
- caching
- deterministic preprocessing
- cheap models for simple extraction

---

## Risk 5 — Privacy

The product handles highly sensitive professional information.

Mitigation:

- least-privilege access
- explicit connectors
- encrypted storage
- source-level permissions
- auditability
- clear retention policy

---

# 56. Open Questions for Architecture Phase

These questions should NOT be solved inside the PRD.

The coding agent/architect should investigate:

1. Which database model is appropriate?
2. Is a relational database sufficient for MVP?
3. Where should embeddings be stored?
4. Is a vector database required immediately?
5. When should GraphRAG be introduced?
6. How should context be chunked?
7. How should people/entity resolution work?
8. How should duplicate events be detected?
9. How should task extraction work?
10. How should commitment confidence be calculated?
11. How should priority scoring be implemented?
12. How should background jobs operate?
13. How should incremental synchronization work?
14. How should Gmail OAuth tokens be managed?
15. How should meeting video processing be performed?
16. What should be synchronous vs asynchronous?
17. What model should handle each task?
18. What should be cached?
19. What should be persisted?
20. What should be retrieved dynamically?
21. How should evaluation be automated?
22. What observability is required?
23. What is the minimum viable deployment architecture?
24. How can the architecture later support Microsoft Graph without rewriting the core?

---

# 57. Definition of Done for MVP

The MVP is complete when a test user can:

1. Sign in with Google.
2. Connect Gmail.
3. Import relevant email.
4. See an email summary.
5. Detect actionable emails.
6. Extract tasks.
7. Extract deadlines.
8. Extract commitments.
9. View open tasks.
10. View reminders.
11. View important people.
12. Ask contextual questions.
13. Receive source-grounded answers.
14. Upload a meeting recording.
15. Receive a meeting summary.
16. Extract meeting tasks and commitments.
17. Ask questions about the meeting.
18. Ask a question connecting the meeting to previous information.
19. See meeting-derived tasks on the dashboard.
20. Receive prioritized items.
21. Correct AI-generated tasks/priorities.
22. Observe that corrections affect future behavior.

---

# 58. Core Product Test

The entire MVP should ultimately answer this question:

> **If a busy CTO stops using the product for three days and comes back, can the product quickly reconstruct what happened, what matters, what they owe, what others owe them, what meetings are coming up, and what they should do next?**

If the answer is yes, the product hypothesis is working.

---

# 59. Product North Star

### **Context Recovery Time**

The amount of time required for the user to understand the relevant context surrounding a work situation.

The product should reduce this over time.

Secondary metrics:

- task extraction accuracy
- deadline accuracy
- commitment accuracy
- retrieval relevance
- answer groundedness
- reminder precision
- priority precision
- user correction rate
- cost per active user
- latency
- daily active usage

---

# 60. Final Product Definition

> **The Executive Context Assistant is an AI-powered personal work companion that continuously connects a user's communications, meetings, people, commitments, tasks, deadlines and decisions, then uses that context to brief, prioritize, remind and assist the user throughout their working day.**

The product is not:

> another chatbot.

It is not:

> another task manager.

It is not:

> another email summarizer.

It is:

> **a continuously maintained context layer for professional work.**

---

# END OF PRD
