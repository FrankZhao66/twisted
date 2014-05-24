# -*- test-case-name: twisted.names.test.test_names,twisted.names.test.test_authority -*-
# Copyright (c) Twisted Matrix Laboratories.
# See LICENSE for details.

"""
Authoritative resolvers.
"""

import os
import time

from twisted.names import dns, error
from twisted.internet import defer
from twisted.internet.interfaces import IResolver
from twisted.python import failure
from twisted.python.compat import execfile

from twisted.names import common

from zope.interface import implementer



@implementer(IResolver)
class MemoryAuthority(common.ResolverBase, object):
    """
    An in-memory authoritative resolver.

    @ivar soa: See C{soa} of L{__init__}.
    @ivar records: See C{records} of L{__init__}.

    @ivar _ADDITIONAL_PROCESSING_TYPES: Record types for which additional
        processing will be done.
    @ivar _ADDRESS_TYPES: Record types which are useful for inclusion in the
        additional section generated during additional processing.
    """
    soa = None
    records = None

    # See https://twistedmatrix.com/trac/ticket/6650
    _ADDITIONAL_PROCESSING_TYPES = (dns.CNAME, dns.MX, dns.NS)
    _ADDRESS_TYPES = (dns.A, dns.AAAA)

    def __init__(self, soa=None, records=None, defaultTTL=None):
        """
        @param soa: The domain name of the start of authority.
        @param records: A dictionary, mapping domain names to lists of authoritative
            records.
        """
        common.ResolverBase.__init__(self)
        if soa is None:
            soa = (b'', None)
        self.soa = soa

        if records is None:
            records = {}
        self.records = records

        self._defaultTTLValue = defaultTTL


    @property
    def _defaultTTL(self):
        """
        Return either a fixed TTL or the highest TTL from the SOA record
        I{minimum} and I{expires} TTL values.

        XXX: Falling back to values from the SOA record is wrong, but left here
        for backwards compatibility. It should instead be a simple instance
        variable which is set from the constructor. See #6661.
        """
        defaultTTL = self._defaultTTLValue
        if defaultTTL is None:
            defaultTTL = max(self.soa[1].minimum, self.soa[1].expire)
        return defaultTTL


    def _additionalRecords(self, answer, authority):
        """
        Find locally known information that could be useful to the consumer of
        the response and construct appropriate records to include in the
        I{additional} section of that response.

        Essentially, implement RFC 1034 section 4.3.2 step 6.

        @param answer: A L{list} of the records which will be included in the
            I{answer} section of the response.

        @param authority: A L{list} of the records which will be included in
            the I{authority} section of the response.

        @return: A generator of L{dns.RRHeader} instances for inclusion in the
            I{additional} section.  These instances represent extra information
            about the records in C{answer} and C{authority}.
        """
        for record in answer + authority:
            if record.type in self._ADDITIONAL_PROCESSING_TYPES:
                name = record.payload.name.name
                for rec in self.records.get(name.lower(), ()):
                    if rec.TYPE in self._ADDRESS_TYPES:
                        yield dns.RRHeader(
                            name, rec.TYPE, dns.IN,
                            rec.ttl or self._defaultTTL, rec, auth=True)


    def _lookup(self, name, cls, type, timeout=None):
        """
        Determine a response to a particular DNS query.

        @param name: The name which is being queried and for which to lookup a
            response.
        @type name: L{bytes}

        @param cls: The class which is being queried.  Only I{IN} is
            implemented here and this value is presently disregarded.
        @type cls: L{int}

        @param type: The type of records being queried.  See the types defined
            in L{twisted.names.dns}.
        @type type: L{int}

        @param timeout: All processing is done locally and a result is
            available immediately, so the timeout value is ignored.

        @return: A L{Deferred} that fires with a L{tuple} of three sets of
            response records (to comprise the I{answer}, I{authority}, and
            I{additional} sections of a DNS response) or with a L{Failure} if
            there is a problem processing the query.
        """
        cnames = []
        results = []
        authority = []
        additional = []

        domain_records = self.records.get(name.lower())

        if domain_records:
            for record in domain_records:
                if record.ttl is not None:
                    ttl = record.ttl
                else:
                    ttl = self._defaultTTL

                if record.TYPE == dns.NS and name.lower() != self.soa[0].lower():
                    # NS record belong to a child zone: this is a referral.  As
                    # NS records are authoritative in the child zone, ours here
                    # are not.  RFC 2181, section 6.1.
                    authority.append(
                        dns.RRHeader(name, record.TYPE, dns.IN, ttl, record, auth=False)
                    )
                elif record.TYPE == type or type == dns.ALL_RECORDS:
                    results.append(
                        dns.RRHeader(name, record.TYPE, dns.IN, ttl, record, auth=True)
                    )
                if record.TYPE == dns.CNAME:
                    cnames.append(
                        dns.RRHeader(name, record.TYPE, dns.IN, ttl, record, auth=True)
                    )
            if not results:
                results = cnames

            # https://tools.ietf.org/html/rfc1034#section-4.3.2 - sort of.
            # See https://twistedmatrix.com/trac/ticket/6732
            additionalInformation = self._additionalRecords(results, authority)
            if cnames:
                results.extend(additionalInformation)
            else:
                additional.extend(additionalInformation)

            if not results and not authority:
                # Empty response. Include SOA record to allow clients to cache
                # this response.  RFC 1034, sections 3.7 and 4.3.4, and RFC 2181
                # section 7.1.
                authority.append(
                    dns.RRHeader(self.soa[0], dns.SOA, dns.IN, ttl, self.soa[1], auth=True)
                    )
            return defer.succeed((results, authority, additional))
        else:
            if dns._isSubdomainOf(name, self.soa[0]):
                # We may be the authority and we didn't find it.
                # XXX: The QNAME may also be a in a delegated child zone. See
                # #6581 and #6580
                return defer.fail(failure.Failure(dns.AuthoritativeDomainError(name)))
            else:
                # The QNAME is not a descendant of this zone. Fail with
                # DomainError so that the next chained authority or
                # resolver will be queried.
                return defer.fail(failure.Failure(error.DomainError(name)))


    def lookupZone(self, name, timeout = 10):
        if self.soa[0].lower() == name.lower():
            # Wee hee hee hooo yea
            if self.soa[1].ttl is not None:
                soa_ttl = self.soa[1].ttl
            else:
                soa_ttl = self._defaultTTL
            results = [dns.RRHeader(self.soa[0], dns.SOA, dns.IN, soa_ttl, self.soa[1], auth=True)]
            for (k, r) in self.records.items():
                for rec in r:
                    if rec.ttl is not None:
                        ttl = rec.ttl
                    else:
                        ttl = self._defaultTTL
                    if rec.TYPE != dns.SOA:
                        results.append(dns.RRHeader(k, rec.TYPE, dns.IN, ttl, rec, auth=True))
            results.append(results[0])
            return defer.succeed((results, (), ()))
        return defer.fail(failure.Failure(dns.DomainError(name)))


    def _cbAllRecords(self, results):
        ans, auth, add = [], [], []
        for res in results:
            if res[0]:
                ans.extend(res[1][0])
                auth.extend(res[1][1])
                add.extend(res[1][2])
        return ans, auth, add



def getSerial(filename = '/tmp/twisted-names.serial'):
    """Return a monotonically increasing (across program runs) integer.

    State is stored in the given file.  If it does not exist, it is
    created with rw-/---/--- permissions.
    """
    serial = time.strftime('%Y%m%d')

    o = os.umask(0177)
    try:
        if not os.path.exists(filename):
            f = file(filename, 'w')
            f.write(serial + ' 0')
            f.close()
    finally:
        os.umask(o)

    serialFile = file(filename, 'r')
    lastSerial, ID = serialFile.readline().split()
    ID = (lastSerial == serial) and (int(ID) + 1) or 0
    serialFile.close()
    serialFile = file(filename, 'w')
    serialFile.write('%s %d' % (serial, ID))
    serialFile.close()
    serial = serial + ('%02d' % (ID,))
    return serial


#class LookupCacherMixin(object):
#    _cache = None
#
#    def _lookup(self, name, cls, type, timeout = 10):
#        if not self._cache:
#            self._cache = {}
#            self._meth = super(LookupCacherMixin, self)._lookup
#
#        if self._cache.has_key((name, cls, type)):
#            return self._cache[(name, cls, type)]
#        else:
#            r = self._meth(name, cls, type, timeout)
#            self._cache[(name, cls, type)] = r
#            return r



class FileAuthority(MemoryAuthority):
    """
    An Authority that is loaded from a file.
    """
    def __init__(self, filename):
        MemoryAuthority.__init__(self)
        self.loadFile(filename)
        self._cache = {}


    def __setstate__(self, state):
        self.__dict__ = state


    def _lookup(self, name, cls, type, timeout=None):
        return MemoryAuthority._lookup(self, name, cls, type, timeout)


    def lookupZone(self, name, timeout=10):
        return MemoryAuthority.lookupZone(self, name, timeout)



class PySourceAuthority(FileAuthority):
    """A FileAuthority that is built up from Python source code."""

    def loadFile(self, filename):
        g, l = self.setupConfigNamespace(), {}
        execfile(filename, g, l)
        if not l.has_key('zone'):
            raise ValueError, "No zone defined in " + filename

        self.records = {}
        for rr in l['zone']:
            if isinstance(rr[1], dns.Record_SOA):
                self.soa = rr
            self.records.setdefault(rr[0].lower(), []).append(rr[1])


    def wrapRecord(self, type):
        return lambda name, *arg, **kw: (name, type(*arg, **kw))


    def setupConfigNamespace(self):
        r = {}
        items = dns.__dict__.iterkeys()
        for record in [x for x in items if x.startswith('Record_')]:
            type = getattr(dns, record)
            f = self.wrapRecord(type)
            r[record[len('Record_'):]] = f
        return r


class BindAuthority(FileAuthority):
    """An Authority that loads BIND configuration files"""

    def loadFile(self, filename):
        self.origin = os.path.basename(filename) + '.' # XXX - this might suck
        lines = open(filename).readlines()
        lines = self.stripComments(lines)
        lines = self.collapseContinuations(lines)
        self.parseLines(lines)


    def stripComments(self, lines):
        return [
            a.find(';') == -1 and a or a[:a.find(';')] for a in [
                b.strip() for b in lines
            ]
        ]


    def collapseContinuations(self, lines):
        L = []
        state = 0
        for line in lines:
            if state == 0:
                if line.find('(') == -1:
                    L.append(line)
                else:
                    L.append(line[:line.find('(')])
                    state = 1
            else:
                if line.find(')') != -1:
                    L[-1] += ' ' + line[:line.find(')')]
                    state = 0
                else:
                    L[-1] += ' ' + line
        lines = L
        L = []
        for line in lines:
            L.append(line.split())
        return filter(None, L)


    def parseLines(self, lines):
        TTL = 60 * 60 * 3
        ORIGIN = self.origin

        self.records = {}

        for (line, index) in zip(lines, range(len(lines))):
            if line[0] == '$TTL':
                TTL = dns.str2time(line[1])
            elif line[0] == '$ORIGIN':
                ORIGIN = line[1]
            elif line[0] == '$INCLUDE': # XXX - oh, fuck me
                raise NotImplementedError('$INCLUDE directive not implemented')
            elif line[0] == '$GENERATE':
                raise NotImplementedError('$GENERATE directive not implemented')
            else:
                self.parseRecordLine(ORIGIN, TTL, line)


    def addRecord(self, owner, ttl, type, domain, cls, rdata):
        if not domain.endswith('.'):
            domain = domain + '.' + owner
        else:
            domain = domain[:-1]
        f = getattr(self, 'class_%s' % cls, None)
        if f:
            f(ttl, type, domain, rdata)
        else:
            raise NotImplementedError, "Record class %r not supported" % cls


    def class_IN(self, ttl, type, domain, rdata):
        record = getattr(dns, 'Record_%s' % type, None)
        if record:
            r = record(*rdata)
            r.ttl = ttl
            self.records.setdefault(domain.lower(), []).append(r)

            print 'Adding IN Record', domain, ttl, r
            if type == 'SOA':
                self.soa = (domain, r)
        else:
            raise NotImplementedError, "Record type %r not supported" % type


    #
    # This file ends here.  Read no further.
    #
    def parseRecordLine(self, origin, ttl, line):
        MARKERS = dns.QUERY_CLASSES.values() + dns.QUERY_TYPES.values()
        cls = 'IN'
        owner = origin

        if line[0] == '@':
            line = line[1:]
            owner = origin
#            print 'default owner'
        elif not line[0].isdigit() and line[0] not in MARKERS:
            owner = line[0]
            line = line[1:]
#            print 'owner is ', owner

        if line[0].isdigit() or line[0] in MARKERS:
            domain = owner
            owner = origin
#            print 'woops, owner is ', owner, ' domain is ', domain
        else:
            domain = line[0]
            line = line[1:]
#            print 'domain is ', domain

        if line[0] in dns.QUERY_CLASSES.values():
            cls = line[0]
            line = line[1:]
#            print 'cls is ', cls
            if line[0].isdigit():
                ttl = int(line[0])
                line = line[1:]
#                print 'ttl is ', ttl
        elif line[0].isdigit():
            ttl = int(line[0])
            line = line[1:]
#            print 'ttl is ', ttl
            if line[0] in dns.QUERY_CLASSES.values():
                cls = line[0]
                line = line[1:]
#                print 'cls is ', cls

        type = line[0]
#        print 'type is ', type
        rdata = line[1:]
#        print 'rdata is ', rdata

        self.addRecord(owner, ttl, type, domain, cls, rdata)
