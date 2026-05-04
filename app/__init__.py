"""FlowXperia voice-agent application package.

Modules:
    config              — single source of truth for all env config (validated)
    prompts             — system prompt for the sales agent
    agent               — long-running LiveKit worker entry-point
    call_dispatcher     — shared place_call() used by CLI + microservice
    make_call           — ad-hoc CLI to place one outbound call
    dispatcher_service  — RabbitMQ-driven call-placement microservice
    enqueue_call        — small CLI to publish one call request to the queue

Run any entry-point as a module from the project root:
    python -m app.agent {console|dev|start}
    python -m app.make_call +919876543210 --name "Krish"
    python -m app.dispatcher_service
    python -m app.enqueue_call +919876543210 --name "Krish"
"""
