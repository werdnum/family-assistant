# Household Data

**What's here:** asking the assistant questions about your household's data lake: health metrics,
bank transactions, Home Assistant history, and message archives.

## What you can ask

If your household runs a data lake in Trino, the assistant can query it with read-only SQL. You
don't need to write the SQL yourself; ask in plain language:

- "How did my resting heart rate change over the last three months?"
- "What did we spend on groceries last month, compared with the month before?"
- "When did the heat pump run longest last week?"
- "Who have I messaged most this month?"

The assistant explores the tables itself, aggregates in the query, and only brings back summaries
and a limited number of rows. It can't change anything in the lake.

## Messages are treated as outside content

Message archives such as WhatsApp hold text written by other people. When a question reads those
tables, the assistant treats the answer the way it treats an email or a web page, so later actions
in the same conversation may ask for your approval. Questions that only touch health, finance or
home data don't have that effect. See [confirmations-and-safety.md](confirmations-and-safety.md) for
how untrusted content is handled.
