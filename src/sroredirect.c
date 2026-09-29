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
 * Per-client rules and the shared wineserver: since Wine 6/7 a winsock
 * connect() is NOT performed by the client process itself - ws2_32 sends an
 * IOCTL_AFD_WINE_CONNECT request and the wineserver calls connect() on the
 * socket (server/sock.c). There is one wineserver per prefix, started by the
 * first wine process with THAT process's environment, so its own
 * SROREDIR_RULES are the first client's. A second client in the same prefix
 * with a different redirect (other proxy port) was therefore redirected with
 * the first client's rules - its connection landed on the wrong proxy and
 * failed. Inside the wineserver the rules are therefore taken from the
 * process that SENT the connect request: the server is single-threaded and
 * reads each request from the requesting thread's request pipe immediately
 * before handling it, so the fd of the last read() is that pipe; the process
 * holding the other end of it (found via /proc/<pid>/fd, cached per pipe)
 * is the client, and its /proc/<pid>/environ holds its own SROREDIR_RULES.
 * A client without SROREDIR_RULES in its environment is never redirected.
 * The redirect line is written into that client's own log (its stderr).
 *
 * Configuration (environment, set by sro.sh):
 *   SROREDIR_RULES   ';'-separated redirect rules, each:
 *                        matchIP,matchPort=dstIP,dstPort
 *                    matchIP may be '*' (any address), matchPort may be 0
 *                    (any port). The first matching rule wins. IPv4 only -
 *                    Silkroad clients connect over IPv4.
 *   SROREDIR_QUIET   if set to 1, do not print the activation/redirect lines.
 *   SROREDIR_DEBUG   if set to 1, also log the parsed rules and lookups.
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
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <unistd.h>

#define MAX_RULES 32
#define PEER_CACHE 64   /* wineserver: remembered request pipes -> client rules */

struct rule {
    unsigned int  match_ip;    /* network byte order; 0 with match_any = any */
    int           match_any;
    unsigned short match_port; /* network byte order; 0 = any port */
    unsigned int  dst_ip;      /* network byte order */
    unsigned short dst_port;   /* network byte order */
};

struct ruleset {
    int         count;
    struct rule r[MAX_RULES];
};

/* One requesting client as seen from the wineserver: the inode of its request
 * pipe, the pid + fd number that holds the other end (to re-validate the
 * cache entry cheaply) and the rules from that process's own environment. */
struct peer {
    int            used;
    unsigned long  ino;
    int            pid;
    int            fd;
    struct ruleset rules;
};

static struct ruleset own;   /* SROREDIR_RULES of THIS process */
static struct peer peers[PEER_CACHE];
static int peer_next;
static int quiet;    /* SROREDIR_QUIET=1: suppress even the redirect-hit line */
static int debug;    /* SROREDIR_DEBUG=1: also log rules + per-process load    */
static int initialized;
static int is_server;          /* this process is a wineserver */
static int last_read_fd = -1;  /* wineserver: fd of the most recent read()   */

typedef int (*connect_t)(int, const struct sockaddr *, socklen_t);
typedef ssize_t (*read_t)(int, void *, size_t);
typedef ssize_t (*read_chk_t)(int, void *, size_t, size_t);
static connect_t real_connect;
static read_t real_read;
static read_chk_t real_read_chk;

static void vlogline(int fd, const char *fmt, va_list ap)
{
    if (quiet) return;
    char buf[256];
    int n = vsnprintf(buf, sizeof(buf), fmt, ap);
    if (n < 0) return;
    if (n > (int)sizeof(buf) - 1) n = sizeof(buf) - 1;
    dprintf(fd, "[sroredirect] %.*s\n", n, buf);
}

static void logline(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    vlogline(2, fmt, ap);
    va_end(ap);
}

/* Log into the stderr of process $pid (its sro.sh log file), so a redirect
 * done by the shared wineserver shows up in the log of the client it belongs
 * to rather than in whichever client happened to start the wineserver. Only
 * regular files are reopened; anything else falls back to our own stderr. */
static void logline_pid(int pid, const char *fmt, ...)
{
    int fd = -1;
    if (pid > 0) {
        char path[64];
        struct stat st;
        snprintf(path, sizeof(path), "/proc/%d/fd/2", pid);
        fd = open(path, O_WRONLY | O_APPEND | O_NOCTTY | O_NONBLOCK | O_CLOEXEC);
        if (fd >= 0 && (fstat(fd, &st) || !S_ISREG(st.st_mode))) {
            close(fd);
            fd = -1;
        }
    }
    va_list ap;
    va_start(ap, fmt);
    vlogline(fd >= 0 ? fd : 2, fmt, ap);
    va_end(ap);
    if (fd >= 0) close(fd);
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

static void parse_rules(const char *spec, struct ruleset *set)
{
    set->count = 0;
    char *copy = strdup(spec);
    if (!copy) return;
    char *save = NULL;
    for (char *tok = strtok_r(copy, ";", &save); tok && set->count < MAX_RULES;
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
        set->r[set->count++] = r;

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

static ssize_t do_read(int fd, void *buf, size_t n)
{
    if (!real_read) real_read = (read_t)dlsym(RTLD_NEXT, "read");
    return real_read ? real_read(fd, buf, n) : syscall(SYS_read, fd, buf, n);
}

/* Rules of process $pid, from SROREDIR_RULES in its /proc/<pid>/environ.
 * A process without the variable (or one that is already gone) gets an empty
 * rule set - it is simply never redirected. */
static void load_pid_rules(int pid, struct ruleset *set)
{
    set->count = 0;
    char path[64];
    snprintf(path, sizeof(path), "/proc/%d/environ", pid);
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return;
    size_t cap = 16384, len = 0;
    char *env = malloc(cap + 1);
    while (env) {
        if (len == cap) {
            char *bigger = realloc(env, cap * 2 + 1);
            if (!bigger) break;
            env = bigger;
            cap *= 2;
        }
        ssize_t n = do_read(fd, env + len, cap - len);
        if (n <= 0) break;
        len += (size_t)n;
    }
    close(fd);
    if (!env) return;
    env[len] = 0;
    static const char key[] = "SROREDIR_RULES=";
    for (size_t off = 0; off < len; off += strlen(env + off) + 1) {
        if (!strncmp(env + off, key, sizeof(key) - 1)) {
            parse_rules(env + off + sizeof(key) - 1, set);
            break;
        }
    }
    free(env);
}

static int fd_links_to(int pid, int fd, const char *want)
{
    char path[64], buf[64];
    snprintf(path, sizeof(path), "/proc/%d/fd/%d", pid, fd);
    ssize_t n = readlink(path, buf, sizeof(buf) - 1);
    if (n <= 0) return 0;
    buf[n] = 0;
    return !strcmp(buf, want);
}

/* The process (other than us) that has an fd pointing at $want ("pipe:[ino]"). */
static int find_pipe_owner(const char *want, int *out_fd)
{
    DIR *proc = opendir("/proc");
    if (!proc) return -1;
    int self = getpid(), found = -1;
    struct dirent *de;
    while (found < 0 && (de = readdir(proc))) {
        if (de->d_name[0] < '1' || de->d_name[0] > '9') continue;
        int pid = atoi(de->d_name);
        if (pid == self) continue;
        char path[64];
        snprintf(path, sizeof(path), "/proc/%d/fd", pid);
        DIR *fds = opendir(path);
        if (!fds) continue;
        struct dirent *fe;
        char buf[64];
        while ((fe = readdir(fds))) {
            if (fe->d_name[0] < '0' || fe->d_name[0] > '9') continue;
            ssize_t n = readlinkat(dirfd(fds), fe->d_name, buf, sizeof(buf) - 1);
            if (n <= 0) continue;
            buf[n] = 0;
            if (!strcmp(buf, want)) {
                found = pid;
                *out_fd = atoi(fe->d_name);
                break;
            }
        }
        closedir(fds);
    }
    closedir(proc);
    return found;
}

/* wineserver only: the rules of the client whose request is being handled
 * right now (see the header comment), or NULL if it could not be determined. */
static const struct ruleset *requester_rules(int fd, int *pid_out)
{
    struct stat st;
    if (fd < 0 || fstat(fd, &st) || !S_ISFIFO(st.st_mode)) return NULL;
    char want[48];
    snprintf(want, sizeof(want), "pipe:[%lu]", (unsigned long)st.st_ino);
    for (int i = 0; i < PEER_CACHE; i++) {
        struct peer *p = &peers[i];
        if (p->used && p->ino == (unsigned long)st.st_ino && fd_links_to(p->pid, p->fd, want)) {
            *pid_out = p->pid;
            return &p->rules;
        }
    }
    int pfd = -1;
    int pid = find_pipe_owner(want, &pfd);
    if (pid < 0) return NULL;
    struct peer *p = &peers[peer_next++ % PEER_CACHE];
    p->used = 1;
    p->ino = (unsigned long)st.st_ino;
    p->pid = pid;
    p->fd = pfd;
    load_pid_rules(pid, &p->rules);
    if (debug) logline_pid(pid, "wineserver: connect request from pid %d (%d rule%s)",
                           pid, p->rules.count, p->rules.count == 1 ? "" : "s");
    *pid_out = pid;
    return &p->rules;
}

static int detect_wineserver(void)
{
    char exe[4096];
    ssize_t n = readlink("/proc/self/exe", exe, sizeof(exe) - 1);
    if (n <= 0) return 0;
    exe[n] = 0;
    const char *base = strrchr(exe, '/');
    base = base ? base + 1 : exe;
    return !strncmp(base, "wineserver", 10);
}

__attribute__((constructor))
static void init(void)
{
    if (initialized) return;
    initialized = 1;
    real_connect = (connect_t)dlsym(RTLD_NEXT, "connect");
    quiet = getenv("SROREDIR_QUIET") && !strcmp(getenv("SROREDIR_QUIET"), "1");
    debug = getenv("SROREDIR_DEBUG") && !strcmp(getenv("SROREDIR_DEBUG"), "1");
    is_server = detect_wineserver();
    const char *spec = getenv("SROREDIR_RULES");
    if (spec && *spec) parse_rules(spec, &own);
    if (debug) logline("loaded%s (%d rule%s)", is_server ? " into wineserver" : "",
                       own.count, own.count == 1 ? "" : "s");
}

/* The wineserver reads every request from the requesting thread's pipe right
 * before handling it; remembering the fd of the last read() is what lets
 * connect() below tell which client it is working for. Just a pass-through
 * in every other process. */
ssize_t read(int fd, void *buf, size_t n)
{
    if (is_server) last_read_fd = fd;
    return do_read(fd, buf, n);
}

/* _FORTIFY_SOURCE builds call this instead of read() for fixed-size buffers. */
ssize_t __read_chk(int fd, void *buf, size_t n, size_t buflen)
{
    if (is_server) last_read_fd = fd;
    if (!real_read_chk) real_read_chk = (read_chk_t)dlsym(RTLD_NEXT, "__read_chk");
    if (real_read_chk) return real_read_chk(fd, buf, n, buflen);
    if (n > buflen) abort();
    return do_read(fd, buf, n);
}

int connect(int fd, const struct sockaddr *addr, socklen_t len)
{
    if (!initialized) init();
    if (real_connect && addr && addr->sa_family == AF_INET &&
        len >= (socklen_t)sizeof(struct sockaddr_in)) {
        const struct ruleset *set = &own;
        int pid = 0;
        if (is_server) {
            const struct ruleset *req = requester_rules(last_read_fd, &pid);
            if (req) set = req;
            else if (debug) logline("wineserver: requesting process not found - using own rules");
        }
        const struct sockaddr_in *in = (const struct sockaddr_in *)addr;
        for (int i = 0; i < set->count; i++) {
            const struct rule *r = &set->r[i];
            if (!r->match_any && r->match_ip != in->sin_addr.s_addr) continue;
            if (r->match_port && r->match_port != in->sin_port) continue;
            struct sockaddr_in redir = *in;
            redir.sin_addr.s_addr = r->dst_ip;
            redir.sin_port = r->dst_port;
            char from[16], to[16];
            struct in_addr fa = { in->sin_addr.s_addr }, ta = { r->dst_ip };
            logline_pid(pid, "redirect %s:%d -> %s:%d",
                        inet_ntop(AF_INET, &fa, from, sizeof(from)), ntohs(in->sin_port),
                        inet_ntop(AF_INET, &ta, to, sizeof(to)), ntohs(r->dst_port));
            return real_connect(fd, (const struct sockaddr *)&redir, sizeof(redir));
        }
    }
    return real_connect ? real_connect(fd, addr, len)
                        : (errno = ENOSYS, -1);
}
