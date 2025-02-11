"""Document summary retrievers.

This module contains retrievers for document summary indices.

"""

from llama_index.callbacks.schema import CBEventType
from llama_index.indices.document_summary.base import GPTDocumentSummaryIndex
from llama_index.indices.query.schema import QueryBundle
from llama_index.indices.base_retriever import BaseRetriever
from typing import Any, List, Optional, Callable, Tuple, Dict
from llama_index.data_structs.node import Node, NodeWithScore
from llama_index.prompts.prompts import QuestionAnswerPrompt
from llama_index.indices.service_context import ServiceContext
from llama_index.indices.query.embedding_utils import (
    get_top_k_embeddings,
)
import logging

logger = logging.getLogger(__name__)


DEFAULT_CHOICE_SELECT_PROMPT_TMPL = (
    "A list of documents is shown below. Each document has a number next to it along "
    "with a summary of the document. A question is also provided. \n"
    "Select the numbers of the documents "
    "you should consult to answer the question, in order of relevance. \n"
    "Do not include any documents that are not relevant to the question. \n"
    "Example format: \n"
    "Document 1: <summary of document 1>\n"
    "Document 2: <summary of document 2>\n"
    "..."
    "Document 10: <summary of document 10>\n"
    "Question: <question>\n"
    "Answer: 9, 7, 4\n\n"
    "Let's try this now: \n"
    "{context_str}\n"
    "Question: {query_str}\n"
    "Answer: "
)
DEFAULT_CHOICE_SELECT_PROMPT = QuestionAnswerPrompt(DEFAULT_CHOICE_SELECT_PROMPT_TMPL)


def default_format_node_batch_fn(
    summary_nodes: List[Node],
) -> str:
    """Default format node batch function."""
    fmt_node_txts = []
    for idx in range(len(summary_nodes)):
        number = idx + 1
        fmt_node_txts.append(f"Document {number}: {summary_nodes[idx].get_text()}")
    return "\n".join(fmt_node_txts)


def default_parse_choice_select_answer_fn(answer: str, num_choices: int) -> List[int]:
    """Default parse choice select answer function."""
    answer_tokens = answer.split(",")
    answer_nums = [int(ans.strip()) for ans in answer_tokens]
    # prune choices based on num_choices
    pruned_answer = [a for a in answer_nums if a <= num_choices]
    return pruned_answer


class DocumentSummaryIndexRetriever(BaseRetriever):
    """Document Summary Index Retriever.

    By default, select relevant summaries from index using LLM calls.

    Args:
        index (GPTDocumentSummaryIndex): The index to retrieve from.

    """

    def __init__(
        self,
        index: GPTDocumentSummaryIndex,
        choice_select_prompt: Optional[QuestionAnswerPrompt] = None,
        choice_batch_size: int = 10,
        format_node_batch_fn: Optional[Callable] = None,
        parse_choice_select_answer_fn: Optional[Callable] = None,
        service_context: Optional[ServiceContext] = None,
        **kwargs: Any,
    ) -> None:
        self._index = index
        self._choice_select_prompt = (
            choice_select_prompt or DEFAULT_CHOICE_SELECT_PROMPT
        )
        self._choice_batch_size = choice_batch_size
        self._format_node_batch_fn = (
            format_node_batch_fn or default_format_node_batch_fn
        )
        self._parse_choice_select_answer_fn = (
            parse_choice_select_answer_fn or default_parse_choice_select_answer_fn
        )
        self._service_context = service_context or index.service_context

    def _retrieve(
        self,
        query_bundle: QueryBundle,
    ) -> List[NodeWithScore]:
        """Retrieve nodes."""
        summary_ids = self._index.index_struct.summary_ids
        results = []
        for idx in range(0, len(summary_ids), self._choice_batch_size):
            summary_ids_batch = summary_ids[idx : idx + self._choice_batch_size]
            summary_nodes = self._index.docstore.get_nodes(summary_ids_batch)
            query_str = query_bundle.query_str
            fmt_batch_str = self._format_node_batch_fn(summary_nodes)
            # call each batch independently
            raw_response, _ = self._service_context.llm_predictor.predict(
                self._choice_select_prompt,
                context_str=fmt_batch_str,
                query_str=query_str,
            )
            raw_choices = self._parse_choice_select_answer_fn(
                raw_response, len(summary_nodes)
            )
            choice_idxs = [choice - 1 for choice in raw_choices]

            choice_summary_ids = [summary_ids_batch[ci] for ci in choice_idxs]

            for summary_id in choice_summary_ids:
                node_ids = self._index.index_struct.summary_id_to_node_ids[summary_id]
                nodes = self._index.docstore.get_nodes(node_ids)
                results.extend([NodeWithScore(n) for n in nodes])

        return results


class DocumentSummaryIndexEmbeddingRetriever(BaseRetriever):
    """Document Summary Index Embedding Retriever.

    Generates embeddings on the fly, attaches to each summary node.

    NOTE: implementation is similar to ListIndexEmbeddingRetriever.

    Args:
        index (GPTDocumentSummaryIndex): The index to retrieve from.

    """

    def __init__(
        self, index: GPTDocumentSummaryIndex, similarity_top_k: int = 1, **kwargs: Any
    ) -> None:
        """Init params."""
        self._index = index
        self._similarity_top_k = similarity_top_k

    def _retrieve(
        self,
        query_bundle: QueryBundle,
    ) -> List[NodeWithScore]:
        """Retrieve nodes."""
        summary_ids = self._index.index_struct.summary_ids
        summary_nodes = self._index.docstore.get_nodes(summary_ids)
        query_embedding, node_embeddings = self._get_embeddings(
            query_bundle, summary_nodes
        )

        _, top_idxs = get_top_k_embeddings(
            query_embedding,
            node_embeddings,
            similarity_top_k=self._similarity_top_k,
            embedding_ids=list(range(len(summary_nodes))),
        )

        top_k_summary_ids = [summary_ids[i] for i in top_idxs]
        results = []
        for summary_id in top_k_summary_ids:
            node_ids = self._index.index_struct.summary_id_to_node_ids[summary_id]
            nodes = self._index.docstore.get_nodes(node_ids)
            results.extend([NodeWithScore(n) for n in nodes])
        return results

    def _get_embeddings(
        self, query_bundle: QueryBundle, nodes: List[Node]
    ) -> Tuple[List[float], List[List[float]]]:
        """Get top nodes by similarity to the query."""
        embed_model = self._index.service_context.embed_model
        if query_bundle.embedding is None:
            event_id = self._index._service_context.callback_manager.on_event_start(
                CBEventType.EMBEDDING
            )
            query_bundle.embedding = embed_model.get_agg_embedding_from_queries(
                query_bundle.embedding_strs
            )
            self._index._service_context.callback_manager.on_event_end(
                CBEventType.EMBEDDING, payload={"num_nodes": 1}, event_id=event_id
            )

        event_id = self._index._service_context.callback_manager.on_event_start(
            CBEventType.EMBEDDING
        )
        id_to_embed_map: Dict[str, List[float]] = {}
        for node in nodes:
            if node.embedding is None:
                embed_model.queue_text_for_embedding(node.get_doc_id(), node.get_text())
            else:
                id_to_embed_map[node.get_doc_id()] = node.embedding

        (
            result_ids,
            result_embeddings,
        ) = embed_model.get_queued_text_embeddings()
        self._index._service_context.callback_manager.on_event_end(
            CBEventType.EMBEDDING,
            payload={"num_nodes": len(result_ids)},
            event_id=event_id,
        )
        for new_id, text_embedding in zip(result_ids, result_embeddings):
            id_to_embed_map[new_id] = text_embedding
        node_embeddings = [id_to_embed_map[n.get_doc_id()] for n in nodes]
        return query_bundle.embedding, node_embeddings
