import json
import re
from typing import Any, Dict, List, Optional

import numpy as np
from langchain_core.prompts import ChatPromptTemplate
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity


class VectorQueryMatcher:
    """使用 TF-IDF 向量化实现的查询匹配器。"""

    def __init__(
        self,
        predefined_cypher_dict: Dict[str, str],
        query_descriptions: Dict[str, str],
        similarity_threshold: float = 0.5,
    ) -> None:
        self.predefined_cypher_dict = predefined_cypher_dict
        self.query_descriptions = query_descriptions
        self.similarity_threshold = similarity_threshold

        # Word-only tokenization treats a Chinese clause as one token, allowing
        # a shared English entity name to select an unrelated relationship.
        self._vectorizer = TfidfVectorizer(analyzer=self._relation_tokens)
        self._query_vectors = self._compute_query_vectors()

    @staticmethod
    def _relation_tokens(text: str) -> List[str]:
        # Entity values are bound separately. They must not decide whether the
        # requested relationship is supplier, category or campaign.
        text = re.sub(
            r"(?<![A-Za-z0-9_-])[A-Z][A-Za-z0-9-]*(?:\s+[A-Z][A-Za-z0-9-]*)+(?![A-Za-z0-9_-])",
            " ", text,
        )
        text = re.sub(r"\b[A-Za-z]+\d+\b", " ", text).lower()
        tokens = re.findall(r"[a-z]+", text)
        for phrase in re.findall(r"[\u4e00-\u9fff]+", text):
            tokens.extend(phrase[index:index + 2] for index in range(len(phrase) - 1))
        return tokens

    def _compute_query_vectors(self) -> Dict[str, np.ndarray]:
        keys: List[str] = []
        corpus: List[str] = []
        for query_name in self.predefined_cypher_dict:
            description = self.query_descriptions.get(query_name) or query_name.replace("_", " ")
            for example in description.splitlines():
                if example.strip():
                    keys.append(query_name)
                    corpus.append(example.strip())

        if not corpus:
            return {}

        matrix = self._vectorizer.fit_transform(corpus).toarray()
        return {key: np.asarray([row for name, row in zip(keys, matrix) if name == key], dtype=np.float32)
                for key in dict.fromkeys(keys)}

    def _embed(self, texts: List[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, len(self._vectorizer.get_feature_names_out())))
        return self._vectorizer.transform(texts).toarray()

    def match_query(self, user_question: str, top_k: int = 3) -> List[Dict[str, Any]]:
        if not user_question or not self._query_vectors or top_k <= 0:
            return []

        question_vector = self._embed([user_question])
        if question_vector.size == 0:
            return []
        question_vector = question_vector[0]

        similarities: List[tuple[str, float]] = []
        for query_name, vector in self._query_vectors.items():
            score = cosine_similarity([question_vector], vector).max()
            similarities.append((query_name, float(score)))

        similarities.sort(key=lambda item: item[1], reverse=True)
        # Generic/ambiguous questions must fall back, not execute whichever
        # template happens to be first in the dictionary.
        if len(similarities) > 1 and similarities[0][1] - similarities[1][1] < 0.1:
            return []

        results: List[Dict[str, Any]] = []
        for query_name, score in similarities[:top_k]:
            if score >= self.similarity_threshold:
                results.append(
                    {
                        "query_name": query_name,
                        "similarity": score,
                        "cypher": self.predefined_cypher_dict[query_name],
                    }
                )
        return results

    def extract_parameters(
        self, user_question: str, query_name: str, llm: Any | None = None
    ) -> Dict[str, str]:
        if query_name not in self.predefined_cypher_dict:
            return {}

        cypher_template = self.predefined_cypher_dict[query_name]
        param_names = re.findall(r"\$(\w+)", cypher_template)
        if not param_names:
            return {}

        # Retail entity names are deterministic in the synthetic graph. Resolve
        # them locally first so predefined queries do not spend an LLM call (and
        # do not depend on the legacy recipe parameter prompt).
        if any(name in {"product_name", "supplier_name", "category_name"} for name in param_names):
            retail_params = self._extract_parameters_with_rules(user_question, param_names)
            if retail_params:
                return retail_params

        if llm is not None:
            llm_params = self._extract_parameters_with_llm(
                user_question, param_names, query_name, llm
            )
            if llm_params:
                return llm_params

        return self._extract_parameters_with_rules(user_question, param_names)

    @staticmethod
    def _extract_parameters_with_rules(
        user_question: str, param_names: List[str]
    ) -> Dict[str, str]:
        params: Dict[str, str] = {}
        for name in param_names:
            if name in {"product_name", "supplier_name", "category_name"}:
                if name == "product_name":
                    sku = re.search(r"(?<![A-Za-z0-9_])SKU\s*(?:为|是|[:：])?\s*([A-Za-z0-9][A-Za-z0-9_-]*)", user_question, re.IGNORECASE)
                    if sku:
                        params[name] = sku.group(1)
                        continue
                # Retail entities are usually emitted as English title-case names
                # in the synthetic graph (for example, ``Aurora ANC Headphones``).
                match = re.search(
                    r"(?<![A-Za-z0-9_-])[A-Z][A-Za-z0-9-]*(?:\s+[A-Z][A-Za-z0-9-]*)+(?![A-Za-z0-9_-])",
                    user_question,
                )
                if match:
                    params[name] = match.group(0).strip()
                else:
                    if name == "category_name":
                        match = re.search(r"\b([A-Z][A-Za-z0-9-]*)\s*(?:品类|分类)", user_question)
                        if match:
                            params[name] = match.group(1).strip()
                            continue
                    marker = "供应商" if name == "supplier_name" else "品类|分类" if name == "category_name" else "商品|产品"
                    match = re.search(rf"(?:{marker})\s*[:：]?\s*([^，。?？]+)", user_question)
                    if match:
                        params[name] = match.group(1).strip()
            elif name == "dish_name":
                match = re.search(r"(?:菜|菜品|做|叫)?([^\s，。,]+)", user_question)
                if match:
                    params[name] = match.group(1)
            elif name == "ingredient_name":
                match = re.search(r"(?:食材|材料|用|加)([^\s，。,]+)", user_question)
                if match:
                    params[name] = match.group(1)
            elif name == "flavor_name":
                match = re.search(r"(麻辣|清淡|酸辣|咸鲜|甜味|香辣)", user_question)
                if match:
                    params[name] = match.group(1)
        return params

    @staticmethod
    def _extract_parameters_with_llm(
        user_question: str,
        param_names: List[str],
        query_name: str,
        llm: Any,
    ) -> Dict[str, str]:
        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "你是参数提取助手，从用户问题中提取指定参数，输出 JSON，不要额外说明。",
                ),
                (
                    "human",
                    f"""用户问题: {user_question}
查询类型: {query_name}
需要提取的参数: {', '.join(param_names)}

请以 JSON 返回，形如: {{"参数名": "参数值"}}""",
                ),
            ]
        )

        response = llm.invoke(prompt.format_prompt())
        content = getattr(response, "content", "") or ""
        try:
            match = re.search(r"{.*}", content, re.DOTALL)
            if not match:
                return {}
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return {
                    str(k): str(v)
                    for k, v in parsed.items()
                    if v is not None and str(v).strip()
                }
        except Exception as exc:  # pragma: no cover - defensive logging
            print(f"无法解析LLM响应为JSON: {exc}")
        return {}


def create_vector_query_matcher(
    predefined_cypher_dict: Dict[str, str],
    query_descriptions: Optional[Dict[str, str]] = None,
) -> VectorQueryMatcher:
    descriptions = query_descriptions or {
        key: key.replace("_", " ") for key in predefined_cypher_dict.keys()
    }
    return VectorQueryMatcher(predefined_cypher_dict, descriptions)
