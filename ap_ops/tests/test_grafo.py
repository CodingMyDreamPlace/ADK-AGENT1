"""Tests del grafo y de la configuracion de los agentes. CERO llamadas al modelo.

Todo lo que se prueba aqui es estructura, no comportamiento: que el grafo este
bien armado, que los agentes no tengan kwargs invalidos, que las claves de
estado no colisionen y que las instrucciones solo interpolen claves que alguien
escribe de verdad.

Son los tests mas rentables de Fase 2: atrapan la clase de error que de otro
modo aparece como un `Fail to load 'ap_ops' module` en `adk web`, o peor, como
un `KeyError` a mitad de una corrida que ya gasto cinco llamadas al modelo.
"""

from __future__ import annotations

import re

import pytest

from ap_ops import config
from ap_ops.coordinador import coordinador
from ap_ops.esquemas import ExpedienteAP
from ap_ops.flujo import join_validadores, pipeline_ap, subflujo_replanteo
from ap_ops.nodos._reintentos import REINTENTOS, REINTENTOS_EN_CICLO
from ap_ops.nodos.plan_accion import agente_critico, agente_plan_accion
from ap_ops.nodos.scoring import agente_scoring_riesgo
from ap_ops.nodos.validadores import CLAVES_VALIDADORES, VALIDADORES

AGENTES_PIPELINE = [*VALIDADORES, agente_scoring_riesgo, agente_plan_accion, agente_critico]
TODOS_LOS_AGENTES = [*AGENTES_PIPELINE, coordinador]


class TestEstructuraDelGrafo:
    def test_el_grafo_valida(self):
        pipeline_ap.graph.validate_graph()
        subflujo_replanteo.graph.validate_graph()

    def test_tiene_los_14_nodos_esperados(self):
        nombres = {n.name for n in pipeline_ap.graph.nodes}
        assert nombres == {
            "__START__",
            "nodo_intake",
            "validador_tres_vias",
            "validador_tributario",
            "validador_duplicados_fraude",
            "validador_proveedor_contrato",
            "join_validadores",
            "nodo_consolidar",
            "agente_scoring_riesgo",
            "agente_plan_accion",
            "agente_critico",
            "compuerta_critico",
            "nodo_decision",
            "nodo_expediente",
        }

    def test_el_fan_out_sale_de_intake_hacia_los_4_validadores(self):
        destinos = {
            e.to_node.name
            for e in pipeline_ap.graph.edges
            if e.from_node.name == "nodo_intake"
        }
        assert destinos == {v.name for v in VALIDADORES}
        assert len(destinos) == 4

    def test_el_join_espera_a_los_4(self):
        origenes = {
            e.from_node.name
            for e in pipeline_ap.graph.edges
            if e.to_node.name == "join_validadores"
        }
        assert origenes == {v.name for v in VALIDADORES}
        assert join_validadores._requires_all_predecessors is True

    def test_el_atajo_touchless_existe_y_esta_enrutado(self):
        """Es la mayor palanca de costo: sin esta arista, una factura limpia
        gastaria 7 llamadas en lugar de 4."""
        atajo = [
            e
            for e in pipeline_ap.graph.edges
            if e.from_node.name == "nodo_consolidar"
            and e.to_node.name == "nodo_decision"
        ]
        assert len(atajo) == 1
        assert atajo[0].route == "sin_hallazgos"

    def test_el_ciclo_del_critico_tiene_una_arista_enrutada(self):
        """ADK rechaza los ciclos 100% incondicionales
        (`_detect_unconditional_cycles`). El ciclo es legal porque la arista de
        retorno lleva route='reintentar'."""
        retorno = [
            e
            for e in pipeline_ap.graph.edges
            if e.from_node.name == "compuerta_critico"
            and e.to_node.name == "agente_plan_accion"
        ]
        assert len(retorno) == 1
        assert retorno[0].route == "reintentar"

    def test_hay_un_solo_nodo_terminal(self):
        """El Workflow admite un unico nodo terminal con output; si hubiera dos,
        ADK levanta WorkflowConfigurationError."""
        con_salida = {e.from_node.name for e in pipeline_ap.graph.edges}
        terminales = {n.name for n in pipeline_ap.graph.nodes} - con_salida
        assert terminales == {"nodo_expediente"}

    def test_la_salida_del_workflow_es_el_expediente(self):
        assert pipeline_ap.output_schema is ExpedienteAP

    def test_la_concurrencia_sale_del_perfil_de_cuota(self):
        """El free tier de AI Studio permite 5 requests/minuto por modelo, y el
        fan-out de 4 validadores son 8 requests en rafaga (cada validador hace
        dos: el function call y la respuesta estructurada). Por eso la
        concurrencia no es una constante: depende del perfil."""
        assert pipeline_ap.max_concurrency == config.MAX_CONCURRENCIA
        assert config.MAX_CONCURRENCIA == (1 if config.ES_FREE_TIER else 4)
        assert pipeline_ap.timeout == config.TIMEOUT_PIPELINE

    def test_sin_state_schema_hasta_fase_6(self):
        """`Workflow._validate_state_schema` rechaza cualquier parametro de
        FunctionNode que no sea campo declarado. Se activa como endurecimiento
        en Fase 6, no antes."""
        assert pipeline_ap.state_schema is None


class TestConfiguracionDeAgentes:
    @pytest.mark.parametrize("agente", TODOS_LOS_AGENTES, ids=lambda a: a.name)
    def test_nombre_es_identificador_python_valido(self, agente):
        """`workflow/_base_node.py` lo exige: sin guiones, espacios ni acentos."""
        assert agente.name.isidentifier(), agente.name

    @pytest.mark.parametrize("agente", TODOS_LOS_AGENTES, ids=lambda a: a.name)
    def test_tiene_descripcion_y_modelo(self, agente):
        assert agente.description
        assert agente.model in (config.MODELO_RAPIDO, config.MODELO_JUICIO)

    @pytest.mark.parametrize("agente", TODOS_LOS_AGENTES, ids=lambda a: a.name)
    def test_la_generacion_va_en_generate_content_config(self, agente):
        """`temperature=` suelto es ValidationError en tiempo de import, y
        `adk web` solo lo reporta como 'Fail to load module'."""
        assert agente.generate_content_config is not None
        assert agente.generate_content_config.temperature is not None
        assert agente.generate_content_config.max_output_tokens

    @pytest.mark.parametrize("agente", AGENTES_PIPELINE, ids=lambda a: a.name)
    def test_todo_agente_del_pipeline_tiene_esquema_y_clave(self, agente):
        assert agente.output_schema is not None, agente.name
        assert agente.output_key, agente.name

    def test_las_claves_de_salida_son_todas_distintas(self):
        """El invariante que evita colisiones en el fan-out: en ADK 2.11 las
        ramas paralelas aislan EVENTOS pero comparten `ctx.state`."""
        claves = [a.output_key for a in AGENTES_PIPELINE]
        assert len(claves) == len(set(claves))

    def test_las_claves_de_los_validadores_coinciden_con_el_mapa(self):
        """`nodo_consolidar` lee por `CLAVES_VALIDADORES`. Si una clave se
        renombra en un lado y no en el otro, el consolidado leeria vacio y
        reportaria los cuatro validadores como no evaluables."""
        reales = {v.output_key for v in VALIDADORES}
        assert reales == set(CLAVES_VALIDADORES)
        nombres = {v.name for v in VALIDADORES}
        assert set(CLAVES_VALIDADORES.values()) == nombres

    @pytest.mark.parametrize("validador", VALIDADORES, ids=lambda a: a.name)
    def test_cada_validador_tiene_exactamente_una_herramienta(self, validador):
        """Una tool por validador mantiene la trayectoria verificable:
        `tool_trajectory_avg_score` puede afirmar que fue llamada."""
        assert len(validador.tools) == 1

    @pytest.mark.parametrize("validador", VALIDADORES, ids=lambda a: a.name)
    def test_los_validadores_tienen_retry_y_timeout(self, validador):
        assert validador.retry_config is not None
        assert validador.retry_config.max_attempts == config.REINTENTO_MAX_INTENTOS
        assert validador.timeout == config.TIMEOUT_VALIDADOR

    def test_el_backoff_supera_la_ventana_de_cuota_en_free_tier(self):
        """El 429 del free tier pide esperar ~30 s. Un `initial_delay=1.0`
        consume los reintentos dentro de la misma ventana ya agotada y el error
        se propaga igual: el retry no sirve de nada si el delay es mas corto
        que la ventana del limite."""
        if not config.ES_FREE_TIER:
            pytest.skip("solo aplica al perfil free")
        assert config.REINTENTO_DELAY_INICIAL >= 25.0
        assert VALIDADORES[0].retry_config.initial_delay >= 25.0

    def test_el_scoring_tiene_retry_porque_no_esta_en_el_ciclo(self):
        """Es la primera llamada a MODELO_JUICIO y el punto donde un 503
        pasajero cuesta mas: tumba el pipeline despues de haber gastado las 8
        llamadas de los validadores."""
        assert agente_scoring_riesgo.retry_config is REINTENTOS
        assert agente_scoring_riesgo.retry_config.exceptions is None

    @pytest.mark.parametrize(
        "agente", [agente_plan_accion, agente_critico], ids=lambda a: a.name
    )
    def test_los_nodos_del_ciclo_tienen_retry_acotado_y_filtrado(self, agente):
        """`retry_config` se MULTIPLICA dentro de un ciclo. Dentro del ciclo del
        critico el retry tiene que estar acotado a 2 intentos y filtrado a
        excepciones transitorias: un 503 no debe tumbar el pipeline, pero un
        error de logica tampoco debe reintentarse en cada vuelta."""
        rc = agente.retry_config
        assert rc is REINTENTOS_EN_CICLO
        assert rc.max_attempts == 2
        assert rc.exceptions, "sin filtro, un error de logica se reintenta por vuelta"
        assert "ServerError" in rc.exceptions
        assert "_ResourceExhaustedError" in rc.exceptions

    def test_el_peor_caso_de_llamadas_esta_acotado(self):
        """Techo de llamadas al modelo por factura, calculado explicitamente.
        Es el numero que decide si el sistema es viable a 10.000 facturas/mes
        o solo en el demo."""
        validadores = len(VALIDADORES) * 2 * config.REINTENTO_MAX_INTENTOS
        scoring = 1 * config.REINTENTO_MAX_INTENTOS
        ciclo = config.MAX_ITER_CRITICO * 2 * 2  # (plan + critico) x 2 intentos
        techo = validadores + scoring + ciclo
        assert techo < 100, f"techo de {techo} llamadas por factura es demasiado"

    def test_el_coordinador_no_tiene_sub_agents(self):
        """Mantener `sub_agents` vacio deja `use_scheduler=False` en el runtime
        de nodos, que es la rama por la que `run_node` funciona de forma simple
        desde el cuerpo de una FunctionTool."""
        assert not coordinador.sub_agents

    def test_el_coordinador_expone_las_4_herramientas(self):
        nombres = {getattr(t, "__name__", getattr(t, "name", "")) for t in coordinador.tools}
        assert nombres == {
            "triar_factura",
            "explicar_decision",
            "replantear_plan",
            "autorizar_pago",
        }


class TestEsquemasAceptadosPorGemini:
    @pytest.mark.parametrize("agente", AGENTES_PIPELINE, ids=lambda a: a.name)
    def test_el_output_schema_es_aceptado_por_gemini(self, agente):
        """Un campo con `gt=` o `lt=` genera exclusiveMinimum/Maximum, que
        `types.Schema` rechaza recien al llamar al modelo: el grafo importa y
        los otros tests pasan, y el pipeline muere despues de gastar 10
        llamadas. Este test lo atrapa gratis."""
        from google.genai import types

        types.Schema.from_json_schema(
            json_schema=types.JSONSchema.model_validate(
                agente.output_schema.model_json_schema()
            )
        )


class TestInterpolacionDeEstado:
    #: Claves que alguien escribe de verdad en `ctx.state`.
    #: nodo_intake: id_factura, nombre_proveedor, moneda, total_factura, factura,
    #:              contexto_validacion, iter_critico
    #: nodo_consolidar: consolidado
    #: agentes via output_key: val_*, evaluacion_riesgo, plan_accion, veredicto_critico
    #: compuerta_critico: instrucciones_mejora_critico, huerfanos_reales
    #: coordinador: instruccion_replanteo, ultimo_expediente_id
    CLAVES_ESCRITAS = {
        "id_factura",
        "nombre_proveedor",
        "moneda",
        "total_factura",
        "factura",
        "contexto_validacion",
        "consolidado",
        "evaluacion_riesgo",
        "plan_accion",
        "veredicto_critico",
        "decision",
        "instruccion_replanteo",
        "instrucciones_mejora_critico",
        "ultimo_expediente_id",
        "iter_critico",
        *CLAVES_VALIDADORES,
    }

    @pytest.mark.parametrize("agente", TODOS_LOS_AGENTES, ids=lambda a: a.name)
    def test_toda_clave_interpolada_existe(self, agente):
        """Una instruccion que interpola `{clave}` sin el `?` levanta KeyError
        en tiempo de ejecucion, despues de haber gastado llamadas al modelo.
        Este test lo atrapa gratis."""
        patron = re.compile(r"(?<!\{)\{([a-zA-Z_][a-zA-Z0-9_]*)\??\}")
        for clave in patron.findall(agente.instruction):
            assert clave in self.CLAVES_ESCRITAS, (
                f"{agente.name} interpola '{{{clave}}}' y nadie escribe esa clave"
            )

    @pytest.mark.parametrize("agente", TODOS_LOS_AGENTES, ids=lambda a: a.name)
    def test_las_claves_opcionales_usan_la_sintaxis_con_interrogacion(self, agente):
        """Claves que pueden no existir todavia (el atajo touchless saltea
        agentes) tienen que interpolarse como `{clave?}` o explotan."""
        opcionales = {
            "instruccion_replanteo",
            "instrucciones_mejora_critico",
            "ultimo_expediente_id",
        }
        for clave in opcionales:
            if f"{{{clave}}}" in agente.instruction:
                pytest.fail(
                    f"{agente.name} usa '{{{clave}}}' sin '?': esa clave puede "
                    f"no existir. Usar '{{{clave}?}}'."
                )


class TestPromptsDeValidadores:
    @pytest.mark.parametrize("validador", VALIDADORES, ids=lambda a: a.name)
    def test_el_prompt_prohibe_recalcular(self, validador):
        """La instruccion mas importante del sistema: la aritmetica es el modo
        de falla numero uno de un LLM en Cuentas por Pagar."""
        ins = validador.instruction.lower()
        assert "unica fuente de verdad" in ins
        assert "no sumes" in ins or "no recalcules" in ins

    @pytest.mark.parametrize("validador", VALIDADORES, ids=lambda a: a.name)
    def test_el_prompt_exige_no_evaluable_ante_falta_de_datos(self, validador):
        ins = validador.instruction.lower()
        assert "no_evaluable" in ins
        assert "no inventes" in ins

    @pytest.mark.parametrize("validador", VALIDADORES, ids=lambda a: a.name)
    def test_el_prompt_pide_el_id_de_factura_del_estado(self, validador):
        assert "{id_factura}" in validador.instruction

    def test_el_critico_conoce_el_umbral_de_cobertura(self):
        assert str(config.UMBRAL_COBERTURA_CRITICO) in agente_critico.instruction

    def test_el_planificador_exige_el_invariante(self):
        ins = agente_plan_accion.instruction
        assert "codigo_hallazgo" in ins
        assert "hallazgos_sin_accion" in ins
        assert "acciones_sin_hallazgo" in ins

    def test_el_coordinador_prohibe_afirmar_que_pago(self):
        """Nunca debe anunciar un pago como hecho: no mueve dinero."""
        assert "autoriz" in coordinador.instruction.lower()
        assert "no mueves" in coordinador.instruction.lower()
