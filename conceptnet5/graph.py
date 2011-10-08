# -*- coding: utf-8 -*-
"""

Concept Net 5
graph.py file from concepnet5 module
written by Rob Speer, Julian Chaidez
Common Sense Computing Group, Medialab
Massachusetts Institute of Technology
Fall 2011

"""
#from neo4jrestclient.client import GraphDatabase, Node
from conceptnet5.config import get_auth
from conceptnet5.whereami import get_project_filename
from pymongo import Connection
import re
import json
import codecs

def list_to_uri_piece(lst):
    """
    Encode a list in a format that is hierarchical yet fits into a URI.

    args:
    lst -- the list which will be encoded

    """
    out_tokens = [u'[/']
    first = True
    for item in lst:
        if first:
            first = False
        else:
            out_tokens.append(u'/,/')
        out_tokens.append(item.strip('/'))
    out_tokens.append(u'/]')
    return u''.join(out_tokens)

def uri_piece_to_list(uri):
    """
    Undo the effect of `list_to_uri_piece`.

    args:
    uri -- the uri to be decoded into a list
    """
    pieces = uri.split(u'/')
    assert pieces[0] == '['
    assert pieces[-1] == ']'
    chunks = []
    current = []
    depth = 0
    for piece in pieces[1:-1]:
        if piece == u',' and depth == 0:
            chunks.append('/' + '/'.join(current))
            current = []
        else:
            current.append(piece)
            if piece == '[':
                depth += 1
            elif piece == ']':
                depth -= 1
    chunks.append('/' + '/'.join(current))
    assert depth == 0
    return chunks

def make_assertion_uri(relation_uri, arg_uri_list):
    """
    creates assertion uri out of component uris
    
    args:
    relation_uri -- the uri of the relation being used i.e 'rel/IsA' or 'en/eat'
    arg_uri_list -- the uris (in list form) of the arguments of the assertion
    i.e ['/en/dog',...]

    """
    return '/assertion/' + list_to_uri_piece([relation_uri] + arg_uri_list)
	    
def make_list_uri(_type, args):
    """
    Creates any list-based uri out of component uris
    
    args:
    _type -- the type of uri being made i.e assertion
    args -- the argument uris i.e ['/en/eat','/en/dog/',..]

    """
    arglist = list_to_uri_piece(args)
    return '/%s/%s' % (_type, arglist)

def normalize_uri(uri):
    """
    Ensure that a URI is in Unicode, strip whitespace that may have crept
    in, and change spaces to underscores, creating URIs that will be
    friendlier to work with later.

    We don't worry about URL-quoting here; the client framework takes
    care of that for us.

    args:
    uri -- the uri being normalized and returned
    """
    if isinstance(uri, str):
        uri = uri.decode('utf-8')
    return uri.strip().replace(u' ', u'_')

class ConceptNetGraph(object):
    """
    This class acts as a container for all of the functions necessary to
    interact with the Concept Net graph database. It has the ability to creata
    Node objects, representing types of nodes like assertions, concepts,
    conjunctions, frames, relations, and sources.  It can also produce
    different types of edges, including justifications, and edges connecting
    assertions with their relations and arguments. Methods in this class can
    also find nodes, generate uris and translate uris, nodes and ids into each
    other.
    """
    def __init__(self, domain):
        """
        initializes ConceptGraph object,
        connects with Neo4j database and calls indexes from that database

        args:
        domain -- url of the database that will be accessed and read by this graph object

        """
        self.connection = Connection(domain, 27017)
        self.db = self.connection.conceptnet

        self.db.nodes.create_index('uri')
        self.db.nodes.create_index('dataset')
        self.db.edges.create_index('key')
        self.db.edges.create_index([('start', 1), ('type', 1)])
        self.db.edges.create_index([('end', 1), ('type', 1)])

    def _create_node_by_type(self, uri, properties = {}):
        """
        creates generic node object,
        parses uri, takes out args, identifies type of node and runs relevant
        method
        
        args:
        uri -- identifier of intended node, used in index
        properties -- (optional) properties for assertions (see assertions)

        """
        # Apply normalization to the URI here. All downstream functions can
        # assume it's normalized.

        uri = normalize_uri(uri)

        if uri.count('/') < 2:
            raise ValueError("""
            The URI %r is too short. You can't create the root or
            a type with this method.
            """ % uri)
        _, _type, rest = uri.split('/', 2)
        method = getattr(self, '_create_%s_node' % _type)
        if method is None:
            raise ValueError("I don't know how to create type %r" % _type)
        return method(uri, rest, properties)

    def _create_node(self, **properties):
        """
        Actually create a node in the graph.
        """
        properties = dict(properties)
        uri = properties['uri']
        self.db.nodes.update({'uri': uri}, properties, upsert=True, safe=True)
        return self.db.nodes.find_one({'uri': uri})

    def _create_edge(self, _type, source, target, properties = {}):
        """
        Create an edge and ensure that it is indexed by its nodes.

        args:
        _type -- the type of edge, i.e 'justifies' or 'normalized'
        source -- the source node of the edge
        target -- the target node of the edge
        properties -- (optional) properties to be attributed to this edge
        """
        properties = dict(properties)
        properties['start'] = self._any_to_uri(source)
        properties['end'] = self._any_to_uri(target)
        properties['type'] = _type
        key = u'%s %s %s' % (_type, source, target)
        properties['key'] = key
        self.db.edges.update({'key': key}, properties, upsert=True, safe=True)
        return self.db.edges.find_one({'key': key})

    def _create_assertion_w_components(self, uri, relation, args, properties):
        """
        A helper function used in creating assertions. Given that the
        relation and args have been found or created as nodes, use them to
	create the assertion.

        args:
        uri -- the uri of the assertion being made
        relation -- the relation being used in this assertion, in any form (uri, node, etc)
        args -- the arguments being used in this assertion, in any form (uri, node, etc)
        properties -- important properties of the assertion

        """
        assertion = self._create_node(
            type='assertion',
            uri=uri,
            score=0,
            **properties
        )
        self._create_edge("relation", assertion, relation)
        for i in xrange(len(args)):
            self._create_edge("arg", assertion, args[i], {'position': i+1})
        return assertion

    def _create_assertion_node(self, uri, rest, properties):
        """
        creates assertion node,
        uses rest as to get relevant component uris and pull up the relevant nodes
        assigns relationships
        creates properties
        returns assertion with parameters

        args:
        uri -- identifier of intended node, used in index
        rest -- relevant parts of uri needed as parameters
        properties -- properties for assertions

        """
        uri_parts = uri_piece_to_list(rest)
        rel_uri = uri_parts[0]
        arg_uris = uri_parts[1:]
        args = []
        rel = self.get_or_create_node(rel_uri)
        for arg_uri in arg_uris:
            args.append(self.get_or_create_node(arg_uri))
        assertion = self._create_assertion_w_components(uri, rel, args,
                                                           properties)

        # We used to set 'normalized' here based on whether we've got a
        # frame. This was a bad idea.
        return assertion

    def _create_concept_node(self, uri, rest, properties):
        """
        creates concept node,
        parses rest argument for language and name
        returns node with concept parameters

        args:
        uri -- identifier of intended node, used in index
        rest -- relevant parts of uri needed as parameters
        properties -- (optional) properties for assertions (see assertions)

        """
        language, name = rest.split('/', 1)
        return self._create_node(
            type='concept',
            language=language,
            name=name.replace('_', ' '),
            uri=uri,
            score=0,
            **properties
        )

    def _create_context_node(self, uri, rest, properties):
        """
        Creates a context node, an abstract node indicating when things are true
        """
        name = rest
        return self._create_node(
            type='context',
            name=name.replace('_', ' '),
            uri=uri,
            score=0,
            **properties
        )
    
    def _create_frame_node(self, uri, rest, properties):
        """
        creates frame node,
        assigns name property
        returns frame (node)

        args:
        uri -- identifier of intended node, used in index
        rest -- relevant parts of uri needed as parameters
        properties -- properties for assertions (see _create_assertion_node function)

        """
        language, name = rest.split('/')
        return self._create_node(
            type='frame',
            name=name.replace('_', ' '),
            language=language,
            score=0,
            uri=uri,
            **properties
        )

    def _create_relation_node(self, uri, rest, properties):
        """
        creates relation node,                                  
        uses rest as relation name
        returns node with relation parameters

        args: 
        uri -- identifier of intended node, used in index
        rest -- relevant parts of uri needed as parameters
        properties -- (optional) properties, mainly for assertions (see assertions)        

        """
        name = rest
        return self._create_node(
            type='relation',
            name=name,
            uri=uri,
            **properties
        )
    
    def _create_source_node(self, uri, rest, properties):
        """
        creates source node,
        used rest as name
        returns node with relation paramaters

        args:
        uri -- identifier of intended node, used in index
        rest -- relevant parts or uri needed in parameters
        properties -- (optional) properties for assertion (see assertions)

        """
        name = rest.split('/')[-1]
        return self._create_node(
            type='source',
            name=name,
            uri=uri,
            **properties
        )

    def _create_web_concept_node(self, uri, properties):
        """
        creates a web concept node, whose uri is the url
        of the web page from which the concept is sourced

        args:
        uri -- identifier of the intended node, used in index
        also the url of the web concept
        rest -- in this case, nonsense
        properties -- optional properties of the web_concept

        """
        return self._create_node(
            type='web_concept',
            uri=uri,
            **properties
        )

    def get_node(self, uri):
        """
        searches for node in main index,
        returns either single Node, None or Error (for multiple results)

        args:
        uri -- the uri of the node in question

        """
        uri = normalize_uri(uri)
        return self.db.nodes.find_one({'uri': uri})

    def find_nodes(self, pattern):
        """
        Search for all nodes whose URIs match a given wildcard pattern,
        using Lucene's wildcard syntax. Returns an iterator of the results.

        See this document for Lucene's syntax:
        http://lucene.apache.org/java/2_0_0/queryparsersyntax.html

        args:
        pattern -- the pattern that is being sought in the node uri

        """
        return self._node_index.query('uri', pattern)

    def get_edge(self, _type, source, target):
        """
        Get an existing edge between two nodes with the specified type, or None
        if it doesn't exist.

        args:
        _type -- the type of edge being sought i.e justifies, normalized etc.
        source -- the source of the edge being sought (the start)
        target -- the target of the edge being sought (the end)

        """
        source = self._any_to_uri(source)
        target = self._any_to_uri(target)
        key = "%s %s %s" % (_type, source, target)
        return self.db.edges.find_one({'key': key})

    def get_incoming_edges(self, node, _type=None):
        """
        Get a generator of (edge, node) pairs for incoming edges to the node.
        """
        if _type is None:
            edges = self.db.edges.find({
                'end': self._any_to_uri(node)
            })
        else:
            edges = self.db.edges.find({
                'end': self._any_to_uri(node),
                'type': _type
            })
        for edge in edges:
            yield edge, edge['start']

    def get_outgoing_edges(self, node, _type=None):
        """
        Get a generator of (edge, node) pairs for outgoing edges from the node.
        """
        if _type is None:
            edges = self.db.edges.find({
                'start': self._any_to_uri(node)
            })
        else:
            edges = self.db.edges.find({
                'start': self._any_to_uri(node),
                'type': _type
            })
        for edge in edges:
            yield edge, edge['end']

    def _any_to_node(self, obj, create=False):
        """
        Converts any given input in the form of an id, uri or node into a node object.

        args:
        obj -- the object to be converted(/made)
        """
        if isinstance(obj, basestring):
            node = self.get_node(obj)
            if node is None:
                if create:
                    node = self.get_or_create_node(obj)
                else:
                    raise ValueError("Could not find node %r" % obj)
            return node
        elif hasattr(obj, '__getitem__'):
            return obj
        else:
            raise TypeError

    def _any_to_uri(self, obj):
        """
        Converts any given input in the form of an id, uri or node into a uri string.

        args:
        obj -- the object to be converted

        """
        if isinstance(obj, basestring):
            return normalize_uri(obj)
        elif hasattr(obj, '__getitem__'):
            return obj['uri']
        elif obj == 0:
            # backwards compatibility
            return u'/'
        else:
            raise TypeError

    def get_node_by_id(self, _id):
        """
        Get a node by its ID in the database.
        """
        raise NotImplementedError

    def get_or_create_node(self, uri, properties = {}):
        """
        tries to find node (by uri), or creates node if it doesn't exist

        args:
        uri -- uri for node in question
        properties -- optional properties for assertion       

        """
        return self.get_node(uri) or self._create_node_by_type(uri, properties)

    def get_or_create_edge(self, _type, source, target, properties = {}):
        """
        Get an edge of the specified `type` between `source` and `target`.
        If it doesn't exist, create it with the given properties.

        args:
        _type -- the type of edge i.e 'justifies', 'normalized' etc.
        source -- the source node of the edge (the start)
        target -- the target node of the edge (the end)
        properties -- (optional) properties that can be attributed to the edge

        """
        return (self.get_edge(_type, source, target) or
                self._create_edge(_type, source, target, properties))

    def get_or_create_assertion(self, relation, args, properties = {}):
        """
        finds or creates assertion using the components of the assertion:
        args, relation etc.
        can take either uri or node, gets one using the other
        convenience function.

        args:
        relation -- relation node in desired assertion
        args -- argument nodes desired in assertion
        properties -- properties for assertion

        """
        uri = make_assertion_uri(self._any_to_uri(relation), \
        [self._any_to_uri(arg) for arg in args])
        return (self.get_node(uri) or 
        self._create_assertion_w_components(uri, self._any_to_node(relation, create=True), \
        [self._any_to_node(arg, create=True) for arg in args], properties))

    def get_or_create_concept(self, language, name, disambiguation=''):
        """
        finds or creates concept using the properties of the concept:
        language and name. convenience function.

        args:
        language -- language code ie. 'en'
        name -- name of concept ie. 'dog','fish' etc
        """
        # handle slashes the same way as spaces, so they don't look like
        # we're disambiguating the concept
        name = name.replace(u'/', u'_')
        uri = u"/concept/%s/%s" % (language, name)
        if disambiguation:
            uri += u'/'+disambiguation
        return self.get_node(uri) or self._create_node_by_type(uri, {})

    def get_or_create_conjunction(self, conjuncts):
        """
        finds or creates a conjunction between nodes
        takes in conjunct arguments and returns conjuntion node

        args:
        conjuncts -- a list of the nodes to be connected to the conjunctions
        """
        uris = [self._any_to_uri(c) for c in conjuncts]
        uris.sort()
        uri = u"/conjunction/" + list_to_uri_piece(uris)
        node = self.get_node(uri)

        # Do we want to use the _create_node_by_type machinery? It doesn't quite fit.
        if node is None:
            node = self._create_node(
                type='conjunction',
                uri=uri
            )
            for conjunct in uris:
                self._create_edge('conjunct', conjunct, node)
        return node
    
    def get_or_create_frame(self, language, name):
        """
        finds of creates frame using name of frame. convenience function.

        args:
        name -- name of frame, ie. "$1 is used for $2"
        """
        name = name.replace(u'/', u'_')
        uri = "/frame/%s/%s" % (language, name)
        return self.get_node(uri) or self._create_node_by_type(uri, {})

    def get_or_create_relation(self, name):
        """
        finds or creates relation using the name of the relation.
        convenience function.

        args:
        name -- name of relation ie. 'IsA'
        """

        uri = "/relation/%s" % name
        return self.get_node(uri) or self._create_node_by_type(uri, {})

    def get_or_create_source(self, source_list):
        """
        finds or creates source using a list of the source uri components.
        convenience function.

        args:
        source_list -- list of source components ex. for '/source/contributor/omcs/bedume'
        source_list would be ['contributor','omcs','bedume']
        """

        uri = normalize_uri("/source/" + "/".join(source_list))
        return self.get_node(uri) or self._create_node_by_type(uri, {})

    def get_or_create_web_concept(self, url):
        """
        finds or creates web concept using the url of that web concept

        args:
        url -- the url of the web concept

        """
        return self.get_node(url) or self._create_web_concept_node(url, {})

    def get_args(self, assertion):
        """
        Given an assertion, get its arguments as a list.
    
        Arguments are represented in the graph as edges of type 'argument',
        with a property called 'position' that will generally either be 1 or 2.
        (People find 1-indexing intuitive in this kind of situation.)

        args:
        assertion -- the assertion (in any form, node, uri etc.) in question
        """
        assertion = self._any_to_uri(assertion)
        edge_pairs = self.get_outgoing_edges(assertion, 'arg')
        edge_pairs.sort(key = lambda pair: pair[0]['position'])
        return [arg for edge, arg in edge_pairs]
    
    def get_rel_and_args(self, assertion):
        """
        Get an assertion's list of both its relation and its arguments.
        """
        assertion = self._any_to_uri(assertion)
        edges = self.get_outgoing_edges(assertion, 'arg')
        edges.sort(key = lambda edge: edge['position'])
        rel = self.get_outgoing_edges(assertion, 'relation')[0][1]
        edge_pairs.sort(key = lambda pair: pair[0]['position'])
        return [rel] + [edge['end'] for edge in edges]

    def justify(self, source, target, weight=1.0):
        """
        Add an edge that justifies (or refutes) `target` using `source`.
        The weight represents the strength of the justification, from
        -1 to 1.

        args:
        source -- the source node of the intended justify edge in any form
        target -- the target node of the justify edge in any form
        weight -- a float 'weight' associated with the justification

        """
        edge = self.get_or_create_edge('justifies', source, target,
                                       {'weight': weight})
        return edge

    def derive_normalized(self, source, target, weight=1.0):
        """
        Add edges indicating that one assertion is derived from another
        through normalization.

        Also adds a justification edge, which should have a positive
        weight.

        args:
        source -- the source node, the 'justifier'
        target -- the target node, the node being justified
        weight -- the weight of the normalized edge
        """

        assert weight > 0
        edge = self.get_or_create_edge('normalized', source, target)
        self.justify(source, target, weight)
        for node1, node2 in zip(self.get_rel_and_args(source),
                                self.get_rel_and_args(target)):
            if not (node1 == node2):
                self.get_or_create_edge('normalized', node1, node2)
        return edge

    def add_context(self, assertion, context):
        """
        Indicate that an assertion is true in a particular context.
        """
        return self.get_or_create_edge('context', assertion, context)

    def delete_node(self, obj):
        """
        This function deletes nodes safely by checking their connections
        and confirming that they are superfluous for the network. It also
        deletes conjunctions.

        args:
        obj -- a uri, id or node object that is the target of the deletion
        """
        raise NotImplementedError

        node = self._any_to_node(obj)
        delete = True
        conj_list = []
        if node['type'] == 'source':
            for relation in concept_graph.get_outgoing_edges(node['uri']):
                if relation['end']['type'] == 'conjunction':
                    conj_list.append(relation['end'])
        elif node['type'] != 'conjunction':
            for relation in concept_graph.get_outgoing_edges(node['uri']):
                if relatiom['start']['type'] == 'assertion':
                    delete = False
                    break
        if delete:
            for edge in concept_graph.get_edges(node['uri']):
                self.db.edges.remove({'key':edge['key']})
            for conjunction in conj_list:
                self.db.nodes.remove({'key':conjunction['key']})
            self.db.nodes.remove({'key':node['key']})
        else: assert False, \
        "There are other nodes that are dependent on this node"

class JSONWriterGraph(ConceptNetGraph):
    """
    Follows the same interface as ConceptNetGraph, but does not actually access
    the database. Instead, it outputs JSON statements to a node file and an
    edge file, which can be batch imported into MongoDB.

    You should run `JSONWriterGraph.close()` when finished, to ensure
    that the files are up-to-date.
    """
    def __init__(self, filename):
        self.filename = filename
        self.nodes = open(filename+'.nodes.json', 'w')
        self.edges = open(filename+'.edges.json', 'w')
        self.recently_created_uris = []

    def _write_node(self, properties):
        print >> self.nodes, json.dumps(properties)

    def _write_edge(self, type, start, end, properties):
        properties = dict(properties)
        properties['start'] = start
        properties['end'] = end
        properties['type'] = type
        properties['key'] = u'%s %s %s' % (type, start, end)
        print >> self.edges, json.dumps(properties)
    
    def _create_node(self, **properties):
        uri = properties['uri']
        if uri in self.recently_created_uris:
            return uri
        self._write_node(properties)

        # put it on a queue of 50 URIs to not recreate
        self.recently_created_uris = self.recently_created_uris[-49:] + [uri]
        return uri

    def _create_edge(self, _type, source, target, properties = {}):
        if source == 0:
            source = '/'
        self._write_edge(_type, source, target, properties)

    def _any_to_uri(self, obj):
        if isinstance(obj, basestring):
            return normalize_uri(obj)
        else:
            raise TypeError

    def _any_to_node(self, obj):
        raise NotImplementedError

    def get_or_create_assertion(self, relation, args, properties = {}):
        uri = make_assertion_uri(self._any_to_uri(relation),
                                 [self._any_to_uri(arg) for arg in args])
        return (self.get_node(uri) or
          self._create_assertion_w_components(uri,
            self.get_or_create_node(relation),
            [self.get_or_create_node(arg) for arg in args],
            properties
          )
        )

    def get_node(self, uri):
        if uri in self.recently_created_uris:
            return uri
        else:
            return None

    def get_edge(self, _type, source, target):
        # force it to be "created"
        return None

    def get_edges(self, source, target):
        return []
    
    def get_args(self, assertion_uri):
        return self.get_rel_and_args(assertion_uri)[1:]

    def get_rel_and_args(self, assertion_uri):
        assert assertion_uri[:11] == '/assertion/'
        rest = assertion_uri[11:]
        return uri_piece_to_list(rest)

    def close(self):
        self.output.close()

class GremlinWriterGraph(ConceptNetGraph):
    """
    Follows the same interface as ConceptNetGraph, but does not actually access
    the database. Instead, it outputs Gremlin statements to a file, which can
    be loaded later on the server, much more quickly.

    You should run `GremlinWriterGraph.close()` when finished, to ensure
    that the file is up-to-date.
    """

    # Find things that look like floating point numbers after a colon.
    # Then wrap them up in `new Float(...)` to protect Java, which will have to
    # eventually deal with these structures, from its own stupidity.
    FLOAT_REGEX = re.compile(ur": ?([-+]?[0-9]*\.[0-9]+)")

    def __init__(self, filename):
        self.filename = filename
        self.output = open(filename, 'w')
        self.recently_created_uris = []    
    
    def _dict_to_gremlin_map(self, thedict):
        if len(thedict) == 0:
            return '[:]'
        str = json.dumps(thedict, ensure_ascii=False).encode('utf-8')
        str = GremlinWriterGraph.FLOAT_REGEX.sub(r": new Float(\1)", str)
        str = str.replace('$', r'\$')
        return '[' + str[1:-1] + ']'

    def _safestr(self, str):
        return "'" + (str.encode('utf-8').replace('$', r'\$').replace("'", r"\'")) + "'"

    def _create_node(self, **properties):
        uri = properties['uri']
        if uri in self.recently_created_uris:
            return uri
        map = self._dict_to_gremlin_map(properties)
        print >> self.output, "Object.metaClass.makeNode(%s, %s)" % \
          (self._safestr(uri), map)

        # put it on a queue of 50 URIs to not recreate
        self.recently_created_uris = self.recently_created_uris[-49:] + [uri]
        return uri

    def _create_edge(self, _type, source, target, properties = {}):
        if source == 0:
            source = '/'
        map = self._dict_to_gremlin_map(properties)
        self.output.write("Object.metaClass.makeEdge(%s, %s, %s, %s)\n" % \
          (self._safestr(_type), self._safestr(source), self._safestr(target), map))

    def _any_to_uri(self, obj):
        if isinstance(obj, basestring):
            return normalize_uri(obj)
        else:
            raise TypeError

    def _any_to_node(self, obj):
        raise NotImplementedError

    def get_or_create_assertion(self, relation, args, properties = {}):
        uri = make_assertion_uri(self._any_to_uri(relation),
                                 [self._any_to_uri(arg) for arg in args])
        return (self.get_node(uri) or
          self._create_assertion_w_components(uri,
            self.get_or_create_node(relation),
            [self.get_or_create_node(arg) for arg in args],
            properties
          )
        )

    def get_node(self, uri):
        if uri in self.recently_created_uris:
            return uri
        else:
            return None

    def get_edge(self, _type, source, target):
        # force it to be "created"
        return None

    def get_edges(self, source, target):
        return []
    
    def get_args(self, assertion_uri):
        return self.get_rel_and_args(assertion_uri)[1:]

    def get_rel_and_args(self, assertion_uri):
        assert assertion_uri[:11] == '/assertion/'
        rest = assertion_uri[11:]
        return uri_piece_to_list(rest)

    def close(self):
        self.output.close()

def get_graph():
    """
    Return a graph object representing the Concept Net graph hosted
    on the tortoise server for the Concept Net team.

    no args
    """
    return ConceptNetGraph('tortoise.csc.media.mit.edu')
