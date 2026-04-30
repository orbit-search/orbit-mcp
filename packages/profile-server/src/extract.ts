const LLM_API_URL = process.env.LLM_API_URL ?? "https://openrouter.ai/api/v1";
const LLM_API_KEY = process.env.LLM_API_KEY ?? "";
const LLM_MODEL = process.env.LLM_MODEL ?? "anthropic/claude-sonnet-4";

export interface ExtractedFact {
  category: string;
  fact: string;
}

export async function extractFacts(messages: string): Promise<ExtractedFact[]> {
  const response = await fetch(`${LLM_API_URL}/chat/completions`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${LLM_API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model: LLM_MODEL,
      messages: [
        {
          role: "system",
          content: `Extract personal facts about the user from the conversation below. Return a JSON array of objects with "category" and "fact" fields.

You MUST use one of these exact category labels:
- basics: personal stats like name, height, weight, nationality, languages spoken
- appearance_and_style: physical features, clothing style, accessories, distinctive visual cues
- aspirations: dreams, personal goals they've mentioned
- brand_preferences: brands they like, stores they shop at, brands they are passionate about
- early_life: growing up, how they were raised, past events that happened to them
- education: school, favorite class, clubs, anything related to education
- family: family members, pets, people significantly close to them
- favorite_places: specific places they like going (restaurants, parks, malls, locations)
- food_and_beverages: food, drinks, specific restaurants they like or dislike
- friends: friends, people they know, descriptions of them
- hobbies_and_interests: non-work skills, talents, collections, sports, video games, cooking, celebrities they're passionate about
- lifestyle: daily routines, exercise, fitness habits, dietary preferences, general lifestyle choices
- media: movies or TV shows they watch
- music: music artists, songs they like, genres they listen to
- personality_traits: positive/negative/neutral qualities, habits, quirks, sense of humor, introvert/extrovert
- possessions: material possessions like car, phone, things they own
- romance: crush, relationship status, romantic interests, boyfriends, girlfriends, exes
- social_media_and_links: social media handles, usernames, personal websites, URLs
- work_history: where they work, what they do for a living, career info, coworkers, dream jobs
- worldview: political leanings, ideological stances, social activism, community issues, religious views
- controversies: controversial facts (not opinions) — scandals, getting in trouble, etc.
- accomplishments: awards, championships, things they're proud of
- none: gibberish or no useful information (use as last resort)

Rules:
- Only extract facts clearly stated or strongly implied about the USER (not other people)
- Each fact should be a single, atomic statement
- Always attempt to use a specific category before falling back to "none"
- If no facts can be extracted, return an empty array []
- Return ONLY valid JSON, no other text`,
        },
        { role: "user", content: messages },
      ],
      temperature: 0,
    }),
  });

  if (!response.ok) {
    const body = await response.text();
    throw new Error(`LLM API error ${response.status}: ${body}`);
  }

  const data = (await response.json()) as {
    choices: Array<{ message: { content: string } }>;
  };

  const content = data.choices[0]?.message?.content ?? "[]";

  try {
    const parsed = JSON.parse(content) as unknown;
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (item): item is ExtractedFact =>
        typeof item === "object" &&
        item !== null &&
        typeof (item as Record<string, unknown>).category === "string" &&
        typeof (item as Record<string, unknown>).fact === "string",
    );
  } catch {
    return [];
  }
}
