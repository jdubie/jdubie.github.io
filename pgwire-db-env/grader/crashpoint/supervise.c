/* Crash-point supervisor: runs a command under a seccomp user-notification filter and SIGKILLs the
 * whole container at the Nth trapped syscall, before that syscall executes.
 *
 *   supervise <port> <cmd> [args...]
 *
 * Trapped ("mutating") syscalls: everything that changes filesystem metadata (create, mkdir, link,
 * rename, unlink, rmdir, chmod, utime*, truncate) plus write/pwrite/writev and fsync/fdatasync/syncfs.
 * They are counted in one global order across every thread and process of the command, because the
 * supervisor receives the notifications one at a time.
 *
 * Control, one line per command on TCP <port> (connections from inside the container are refused):
 *   arm N   -> reset the count; kill when the count reaches N (N = 0: count only)   replies "ok"
 *   count   -> trapped syscalls since the last arm                                 replies "<n>"
 * When the command exits, the supervisor exits with its status.
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <netinet/in.h>
#include <poll.h>
#include <signal.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

static const int TRAP_ALWAYS[] = {
    SYS_write, SYS_pwrite64, SYS_writev, SYS_pwritev, SYS_pwritev2, SYS_fsync, SYS_fdatasync, SYS_syncfs,
    SYS_sync, SYS_sync_file_range, SYS_creat, SYS_rename, SYS_renameat, SYS_renameat2, SYS_mkdir, SYS_mkdirat,
    SYS_rmdir, SYS_link, SYS_linkat, SYS_unlink, SYS_unlinkat, SYS_chmod, SYS_fchmod, SYS_fchmodat, 452 /* fchmodat2 */,
    SYS_utime, SYS_utimes, SYS_futimesat, SYS_utimensat, SYS_truncate, SYS_ftruncate, SYS_openat2,
};
#define NA (int)(sizeof TRAP_ALWAYS / sizeof TRAP_ALWAYS[0])
#define CREATE_FLAGS (O_CREAT | 020000000 /* __O_TMPFILE */)

static int install_filter(void) {
    struct sock_filter f[2 * NA + 16];
    int n = 0;
    f[n++] = (struct sock_filter)BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, arch));
    f[n++] = (struct sock_filter)BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, AUDIT_ARCH_X86_64, 1, 0);
    f[n++] = (struct sock_filter)BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW);
    f[n++] = (struct sock_filter)BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr));
    for (int i = 0; i < NA; i++) {
        f[n++] = (struct sock_filter)BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, TRAP_ALWAYS[i], 0, 1);
        f[n++] = (struct sock_filter)BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_USER_NOTIF);
    }
    /* open(path, flags): flags in args[1]; openat(dfd, path, flags): flags in args[2] */
    f[n++] = (struct sock_filter)BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_open, 0, 2);
    f[n++] = (struct sock_filter)BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, args[1]));
    f[n++] = (struct sock_filter)BPF_JUMP(BPF_JMP | BPF_JA, 2, 0, 0); /* to the JSET */
    f[n++] = (struct sock_filter)BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_openat, 0, 4);
    f[n++] = (struct sock_filter)BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, args[2]));
    f[n++] = (struct sock_filter)BPF_STMT(BPF_ALU | BPF_AND | BPF_K, 0); /* placeholder, replaced below */
    f[n - 1] = (struct sock_filter)BPF_JUMP(BPF_JMP | BPF_JSET | BPF_K, CREATE_FLAGS, 1, 0);
    f[n++] = (struct sock_filter)BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW);
    f[n++] = (struct sock_filter)BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_USER_NOTIF);
    f[n++] = (struct sock_filter)BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW);
    struct sock_fprog prog = {.len = (unsigned short)n, .filter = f};
    return (int)syscall(SYS_seccomp, SECCOMP_SET_MODE_FILTER, SECCOMP_FILTER_FLAG_NEW_LISTENER, &prog);
}

static void send_fd(int sock, int fd) {
    char c = 'f', buf[CMSG_SPACE(sizeof(int))];
    struct iovec iov = {&c, 1};
    struct msghdr m = {.msg_iov = &iov, .msg_iovlen = 1, .msg_control = buf, .msg_controllen = sizeof buf};
    struct cmsghdr *h = CMSG_FIRSTHDR(&m);
    h->cmsg_level = SOL_SOCKET;
    h->cmsg_type = SCM_RIGHTS;
    h->cmsg_len = CMSG_LEN(sizeof(int));
    memcpy(CMSG_DATA(h), &fd, sizeof(int));
    if (sendmsg(sock, &m, 0) != 1) _exit(120);
}

static int recv_fd(int sock) {
    char c, buf[CMSG_SPACE(sizeof(int))];
    struct iovec iov = {&c, 1};
    struct msghdr m = {.msg_iov = &iov, .msg_iovlen = 1, .msg_control = buf, .msg_controllen = sizeof buf};
    if (recvmsg(sock, &m, 0) != 1) return -1;
    struct cmsghdr *h = CMSG_FIRSTHDR(&m);
    if (!h) return -1;
    int fd;
    memcpy(&fd, CMSG_DATA(h), sizeof(int));
    return fd;
}

static int local_peer(int c) {
    struct sockaddr_in a = {0}, b = {0};
    socklen_t la = sizeof a, lb = sizeof b;
    if (getpeername(c, (struct sockaddr *)&a, &la) || getsockname(c, (struct sockaddr *)&b, &lb)) return 1;
    if ((ntohl(a.sin_addr.s_addr) >> 24) == 127) return 1;
    return a.sin_addr.s_addr == b.sin_addr.s_addr;
}

int main(int argc, char **argv) {
    if (argc < 3) {
        fprintf(stderr, "usage: supervise <port> <cmd> [args...]\n");
        return 2;
    }
    int port = atoi(argv[1]);
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)) { perror("no_new_privs"); return 2; }
    int sp[2];
    if (socketpair(AF_UNIX, SOCK_STREAM, 0, sp)) { perror("socketpair"); return 2; }
    pid_t child = fork();
    if (child == 0) {
        close(sp[0]);
        int lfd = install_filter();
        if (lfd < 0) { perror("seccomp"); _exit(121); }
        send_fd(sp[1], lfd);
        close(lfd);
        close(sp[1]);
        execvp(argv[2], argv + 2);
        perror("exec");
        _exit(127);
    }
    close(sp[1]);
    int nfd = recv_fd(sp[0]);
    if (nfd < 0) { fprintf(stderr, "supervise: no listener fd\n"); return 2; }
    int pfd = (int)syscall(SYS_pidfd_open, child, 0);
    int ls = socket(AF_INET, SOCK_STREAM, 0), one = 1;
    setsockopt(ls, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    struct sockaddr_in addr = {.sin_family = AF_INET, .sin_port = htons(port), .sin_addr.s_addr = INADDR_ANY};
    if (bind(ls, (struct sockaddr *)&addr, sizeof addr) || listen(ls, 8)) { perror("control socket"); return 2; }

    long long count = 0, target = 0;
    int cl = -1;
    char line[128];
    int ll = 0;
    for (;;) {
        struct pollfd p[4] = {{nfd, POLLIN, 0}, {pfd, POLLIN, 0}, {ls, POLLIN, 0}, {cl, POLLIN, 0}};
        if (poll(p, cl >= 0 ? 4 : 3, -1) < 0) {
            if (errno == EINTR) continue;
            return 3;
        }
        if (p[0].revents & POLLIN) {
            struct seccomp_notif req;
            memset(&req, 0, sizeof req);
            if (ioctl(nfd, SECCOMP_IOCTL_NOTIF_RECV, &req) == 0) {
                count++;
                if (target > 0 && count == target) {
                    kill(-1, SIGKILL); /* every process we may signal, except ourselves and init */
                    _exit(137);
                }
                struct seccomp_notif_resp resp = {.id = req.id, .val = 0, .error = 0, .flags = SECCOMP_USER_NOTIF_FLAG_CONTINUE};
                ioctl(nfd, SECCOMP_IOCTL_NOTIF_SEND, &resp); /* ENOENT if the caller died meanwhile */
            }
        }
        if (p[1].revents & POLLIN) {
            int st = 0;
            waitpid(child, &st, 0);
            return WIFEXITED(st) ? WEXITSTATUS(st) : 128 + WTERMSIG(st);
        }
        if (p[2].revents & POLLIN) {
            int c = accept(ls, NULL, NULL);
            if (c >= 0) {
                if (local_peer(c) || cl >= 0) close(c);
                else { cl = c; ll = 0; }
            }
        }
        if (cl >= 0 && (p[3].revents & (POLLIN | POLLHUP | POLLERR))) {
            ssize_t r = read(cl, line + ll, sizeof line - 1 - ll);
            if (r <= 0) { close(cl); cl = -1; continue; }
            ll += (int)r;
            char *nl;
            while ((nl = memchr(line, '\n', ll))) {
                *nl = 0;
                char out[64];
                if (!strncmp(line, "arm ", 4)) { target = atoll(line + 4); count = 0; snprintf(out, sizeof out, "ok\n"); }
                else if (!strcmp(line, "count")) snprintf(out, sizeof out, "%lld\n", count);
                else snprintf(out, sizeof out, "?\n");
                if (write(cl, out, strlen(out)) < 0) { /* client went away */ }
                int used = (int)(nl - line) + 1;
                memmove(line, nl + 1, ll - used);
                ll -= used;
            }
            if (ll >= (int)sizeof line - 1) ll = 0;
        }
    }
}
