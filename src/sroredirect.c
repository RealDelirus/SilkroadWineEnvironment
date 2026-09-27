/* SPDX-License-Identifier: GPL-3.0-or-later */
/*
 * sroredirect - transparent connection redirect for a Silkroad client.
 *
 * This is the Linux-native counterpart to the Redirect_Gateway / Redirect_Agent
 * options of edxSilkroadLoader5. The EDX loader is a Windows DLL that has to
 * hook ws2_32!connect INSIDE the client because that is the only hook point on
 * Windows. Here the client runs under Wine, so we do the same thing the way
 * proxychains/tsocks do it on Linux: an LD_PRELOAD library that wraps libc
 * connect() for the launched Wine process only, and rewrites the destination
 * of the client's gateway/agent connection so it lands on a local bot proxy
 * (phBot, RBC, ...) instead of the game server. Everything else is passed
 * through untouched. Nothing is injected into the client, nothing needs root,
 * and the system resolver is not modified.
 *
 * Scope is exactly one process tree: sro.sh sets LD_PRELOAD + the rules below
 * only in the environment of the wine command it launches, so no other program
 * on the machine is affected.
 *
 * Configuration (environment, set by sro.sh):
 *   SROREDIR_RULES   ';'-separated redirect rules, each:
 *                        matchIP,matchPort=dstIP,dstPort
 *                    matchIP may be '*' (any address), matchPort may be 0
 *                    (any port). The first matching rule wins. IPv4 only -
 *                    Silkroad clients connect over IPv4.
 *   SROREDIR_QUIET   if set to 1, do not print the activation/redirect lines.
 *
 * Example (redirect the Cyron gateway to a local proxy on 15779):
 *   SROREDIR_RULES='203.0.113.10,15779=127.0.0.1,15779'
 *
 * Build (both arches, so it works whether the wine process is 32- or 64-bit):
 *   gcc      -O2 -fPIC -shared -o lib64/sroredirect.so src/sroredirect.c -ldl
 *   gcc -m32 -O2 -fPIC -shared -o lib/sroredirect.so   src/sroredirect.c -ldl
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <arpa/inet.h>
#include <errno.h>
#include <stdarg.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <unistd.h>

#define MAX_RULES 32

struct rule {
    unsigned int  match_ip;    /* network byte order; 0 with match_any = any */
    int           match_any;
    unsigned short match_port; /* network byte order; 0 = any port */
    unsigned int  dst_ip;      /* network byte order */
    unsigned short dst_port;   /* network byte order */
};

static struct rule rules[MAX_RULES];
static int rule_count;
static int quiet;    /* SROREDIR_QUIET=1: suppress even the redirect-hit line */
static int debug;    /* SROREDIR_DEBUG=1: also log rules + per-process load    */
static int initialized;

typedef int (*connect_t)(int, const struct sockaddr *, socklen_t);
static connect_t real_connect;

static void logline(const char *fmt, ...)
{
    if (quiet) return;
    char buf[256];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    if (n < 0) return;
    if (n > (int)sizeof(buf) - 1) n = sizeof(buf) - 1;
    fprintf(stderr, "[sroredirect] %.*s\n", n, buf);
}

/* Parse "ip,port" into network-order ip (or *) and port. Returns 0 on error. */
static int parse_endpoint(char *s, unsigned int *ip, int *any, unsigned short *port)
{
    char *comma = strchr(s, ',');
    if (!comma) return 0;
    *comma = 0;
    char *ips = s, *ports = comma + 1;
    if (strcmp(ips, "*") == 0) {
        *any = 1;
        *ip = 0;
    } else {
        struct in_addr a;
        if (inet_pton(AF_INET, ips, &a) != 1) return 0;
        *any = 0;
        *ip = a.s_addr;
    }
    int p = atoi(ports);
    if (p < 0 || p > 65535) return 0;
    *port = htons((unsigned short)p);
    return 1;
}

static void parse_rules(const char *spec)
{
    char *copy = strdup(spec);
    if (!copy) return;
    char *save = NULL;
    for (char *tok = strtok_r(copy, ";", &save); tok && rule_count < MAX_RULES;
         tok = strtok_r(NULL, ";", &save)) {
        char *eq = strchr(tok, '=');
        if (!eq) continue;
        *eq = 0;
        struct rule r;
        memset(&r, 0, sizeof(r));
        unsigned int dip; int dany; unsigned short dport;
        if (!parse_endpoint(tok, &r.match_ip, &r.match_any, &r.match_port))
            continue;
        if (!parse_endpoint(eq + 1, &dip, &dany, &dport) || dany)
            continue;   /* the destination must be a concrete address */
        r.dst_ip = dip;
        r.dst_port = dport;
        rules[rule_count++] = r;

        if (!debug) continue;
        char a[16], b[16];
        struct in_addr ma = { r.match_ip }, da = { r.dst_ip };
        logline("rule: %s:%d -> %s:%d",
                r.match_any ? "*" : inet_ntop(AF_INET, &ma, a, sizeof(a)),
                ntohs(r.match_port),
                inet_ntop(AF_INET, &da, b, sizeof(b)),
                ntohs(r.dst_port));
    }
    free(copy);
}

__attribute__((constructor))
static void init(void)
{
    if (initialized) return;
    initialized = 1;
    real_connect = (connect_t)dlsym(RTLD_NEXT, "connect");
    quiet = getenv("SROREDIR_QUIET") && !strcmp(getenv("SROREDIR_QUIET"), "1");
    debug = getenv("SROREDIR_DEBUG") && !strcmp(getenv("SROREDIR_DEBUG"), "1");
    const char *spec = getenv("SROREDIR_RULES");
    if (spec && *spec) parse_rules(spec);
    if (debug) logline("loaded (%d rule%s)", rule_count, rule_count == 1 ? "" : "s");
}

int connect(int fd, const struct sockaddr *addr, socklen_t len)
{
    if (!initialized) init();
    if (real_connect && addr && addr->sa_family == AF_INET &&
        len >= (socklen_t)sizeof(struct sockaddr_in) && rule_count) {
        const struct sockaddr_in *in = (const struct sockaddr_in *)addr;
        for (int i = 0; i < rule_count; i++) {
            const struct rule *r = &rules[i];
            if (!r->match_any && r->match_ip != in->sin_addr.s_addr) continue;
            if (r->match_port && r->match_port != in->sin_port) continue;
            struct sockaddr_in redir = *in;
            redir.sin_addr.s_addr = r->dst_ip;
            redir.sin_port = r->dst_port;
            char from[16], to[16];
            struct in_addr fa = { in->sin_addr.s_addr }, ta = { r->dst_ip };
            logline("redirect %s:%d -> %s:%d",
                    inet_ntop(AF_INET, &fa, from, sizeof(from)), ntohs(in->sin_port),
                    inet_ntop(AF_INET, &ta, to, sizeof(to)), ntohs(r->dst_port));
            return real_connect(fd, (const struct sockaddr *)&redir, sizeof(redir));
        }
    }
    return real_connect ? real_connect(fd, addr, len)
                        : (errno = ENOSYS, -1);
}
