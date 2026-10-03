import type { ContextCard } from "@/lib/types";

const DATA_CLASS_LABEL: Record<string, string> = {
  source: "From your sources",
  ai_derived: "AI-derived",
  user: "Set or confirmed by you",
  computed: "Computed",
};

/** One deterministic context card with its provenance label and evidence links. */
export function ContextCardRow({ card }: { card: ContextCard }) {
  return (
    <li className="card">
      <p className="title">{card.line}</p>
      <p className="meta">
        <span className={card.data_class === "ai_derived" ? "label ai" : "label"}>
          {DATA_CLASS_LABEL[card.data_class] ?? card.data_class}
        </span>
        {card.source_item_ids.length > 0 && (
          <>
            {" · "}
            {card.source_item_ids.map((id, i) => (
              <a key={id} href={`/api/v1/sources/${id}`} target="_blank" rel="noreferrer">
                source {i + 1}
              </a>
            ))}
          </>
        )}
      </p>
    </li>
  );
}
