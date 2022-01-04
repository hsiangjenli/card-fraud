import json
import logging
import os
import time
from argparse import ArgumentParser
from flask import Flask, render_template, request, Response
from gqlalchemy import Memgraph
from random import randint, sample


MEMGRAPH_HOST = os.getenv("MEMGRAPH_HOST", "memgraph")
MEMGRAPH_PORT = int(os.getenv("MEMGRAPH_PORT", "7687"))

log = logging.getLogger(__name__)


def init_log():
    logging.basicConfig(level=logging.INFO)
    log.info("Logging enabled")
    # Set the log level for werkzeug to WARNING because it will print out too much info otherwise
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


# Parse the input arguments for the app
def parse_args():
    '''
    Parse command line arguments.
    '''
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0",
                        help="Allowed host addresses.")
    parser.add_argument("--port", default=5000, type=int,
                        help="App port.")
    parser.add_argument("--template-folder", default="public/template",
                        help="The folder with flask templates.")
    parser.add_argument("--static-folder", default="public",
                        help="The folder with flask static files.")
    parser.add_argument("--debug", default=True, action="store_true",
                        help="Run web server in debug mode")
    print(__doc__)
    return parser.parse_args()


args = parse_args()
memgraph = None

# Create the Flask server instance
app = Flask(__name__,
            template_folder=args.template_folder,
            static_folder=args.static_folder,
            static_url_path='')


def init_data(card_count, pos_count):
    """Populate the database with initial Card and POS device entries."""

    log.info("Initializing {} cards and {} POS devices".format(
        card_count, pos_count))
    start_time = time.time()

    memgraph.execute("UNWIND range(0, {} - 1) AS id "
                     "CREATE (:Card {{id: id, compromised: false}})".format(
                         card_count))
    memgraph.execute("UNWIND range(0, {} - 1) AS id "
                     "CREATE (:Pos {{id: id, compromised: false}})".format(
                         pos_count))

    log.info("Initialized data in %.2f sec", time.time() - start_time)


def compromise_pos(pos_id):
    """Mark a POS device as compromised."""

    memgraph.execute(
        "MATCH (p:Pos {{id: {}}}) SET p.compromised = true".format(pos_id))
    log.info("Point of sale %d is compromised", pos_id)


def compromise_pos_devices(pos_count, fraud_count):
    """Compromise a number of random POS devices."""

    log.info("Compromising {} out of {} POS devices".format(
        fraud_count, pos_count))
    start_time = time.time()

    compromised_devices = sample(range(pos_count), fraud_count)
    for pos_id in compromised_devices:
        compromise_pos(pos_id)

    log.info("Compromisation took %.2f sec", time.time() - start_time)


def pump_transactions(card_count, pos_count, tx_count, report_pct):
    """Create transactions. If the POS device is compromised, 
    then the card in the transaction gets compromised too. 
    If the card is compromised, there is a 0.1% chance the 
    transaction is fraudulent and detected (regardless of 
    the POS device)."""

    log.info("Creating {} transactions".format(tx_count))
    start_time = time.time()

    query = ("MATCH (c:Card {{id: {}}}), (p:Pos {{id: {}}}) "
             "CREATE (t:Transaction "
             "{{id: {}, fraudReported: c.compromised AND (rand() < %f)}}) "
             "CREATE (c)<-[:Using]-(t)-[:At]->(p) "
             "SET c.compromised = p.compromised" % report_pct)

    def rint(max): return randint(0, max - 1)
    for i in range(tx_count):
        memgraph.execute(query.format(rint(card_count),
                                      rint(pos_count),
                                      i))

    duration = time.time() - start_time
    log.info("Created %d transactions in %.2f seconds", tx_count, duration)


@app.route('/resolve-pos', methods=['POST'])
def resolve_pos():
    """Resolve a POS device and card as not compromised."""

    data = request.get_json(silent=True)
    start_time = time.time()

    memgraph.execute("MATCH (p:Pos {{id: {}}}) "
                     "SET p.compromised = false "
                     "WITH p MATCH (p)--(t:Transaction)--(c:Card) "
                     "SET t.fraudReported = false, c.compromised = false".format(data['pos']))

    duration = time.time() - start_time
    log.info("Compromised Point of sale %s has been resolved in %.2f sec",
             data['pos'], duration)

    response = {"duration": duration}
    return Response(
        json.dumps(response),
        status=200,
        mimetype='application/json')


@app.route('/get-compromised-pos', methods=['GET'])
def get_compromised_pos():
    """Get compromised POS devices."""

    log.info("Getting compromised Point Of Service IDs")
    start_time = time.time()

    data = memgraph.execute_and_fetch("MATCH (t:Transaction {fraudReported: true})-[:Using]->(:Card)"
                                      "<-[:Using]-(:Transaction)-[:At]->(p:Pos) "
                                      "WITH p.id as pos, count(t) as connected_frauds "
                                      "WHERE connected_frauds > 1 "
                                      "RETURN pos, connected_frauds ORDER BY connected_frauds DESC")
    data = list(data)

    log.info("Found %d POS with more then one fraud in %.2f sec",
             len(data), time.time() - start_time)

    return json.dumps(data)


@app.route('/get-fraudulent-transactions', methods=['GET'])
def get_fraudulent_transactions():
    """Get fraudulent transactions."""

    log.info("Getting fraudulent transactions")
    start_time = time.time()

    data = memgraph.execute_and_fetch(
        "MATCH (t:Transaction {fraudReported: true}) RETURN t.id as id")
    data = list(data)

    duration = time.time() - start_time
    log.info("Found %d fraudulent transactions in %.2f",
             len(data), duration)

    response = {"duration": duration, "fraudulent_txs": data}
    return Response(
        json.dumps(response),
        status=200,
        mimetype='application/json')


@app.route('/generate-data', methods=['POST'])
def generate_data():
    """Initialize the database."""

    data = request.get_json(silent=True)

    if data['pos'] < data['frauds']:
        return Response(
            json.dumps(
                {'error': "There can't be more frauds than devices"}),
            status=418,
            mimetype='application/json')

    start_time = time.time()

    memgraph.drop_database()
    init_data(data['cards'], data['pos'])
    compromise_pos_devices(data['pos'], data['frauds'])
    pump_transactions(data['cards'], data['pos'],
                      data['transactions'], data['reports'])

    duration = time.time() - start_time

    response = {"duration": duration}
    return Response(
        json.dumps(response),
        status=201,
        mimetype='application/json')


@app.route('/pos-graph', methods=['POST'])
def host():
    log.info("Client fetching POS connected components")

    request_data = request.get_json(silent=True)

    data = memgraph.execute_and_fetch("MATCH (p1:Pos)<-[:At]-(t1:Transaction {{fraudReported: true}})-[:Using] "
                                      "->(c:Card)<-[:Using]-(t2:Transaction)-[:At]->(p2:Pos {{id: {}}})"
                                      "RETURN p1, t1, c, t2, p2".format(request_data['pos']))
    data = list(data)

    output = []
    for item in data:
        p1 = item['p1'].properties
        t1 = item['t1'].properties
        c = item['c'].properties
        t2 = item['t2'].properties
        p2 = item['p2'].properties
        print(p2)
        output.append({'p1': p1, 't1': t1, 'c': c, 't2': t2, 'p2': p2})

    return Response(
        json.dumps(output),
        status=200,
        mimetype='application/json')


# Retrieve the home page for the app
@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')


@app.route('/graph', methods=['GET'])
def graph():
    return render_template('graph.html',
                           pos=request.args.get('pos'),
                           frauds=request.args.get('frauds'))


# Entrypoint for the app that will be executed first
def main():
    # Code that should only be run once
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        init_log()

        global memgraph
        memgraph = Memgraph(MEMGRAPH_HOST,
                            MEMGRAPH_PORT)
        
        memgraph.execute("CREATE INDEX ON :Card(id)")
        memgraph.execute("CREATE INDEX ON :Pos(id)")
        memgraph.execute("CREATE INDEX ON :Transaction(fraudReported)")
    app.run(host=args.host,
            port=args.port,
            debug=args.debug)


if __name__ == "__main__":
    main()
